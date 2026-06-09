#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/printk.h>
#include <zephyr/usb/usb_device.h>

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/uuid.h>

/* ── Tunables ─────────────────────────────────────────────────────────────── */
#define MAX_CONNECTIONS  15  /* must match CONFIG_BT_MAX_CONN in prj.conf     */
#define MAX_SERVICES      6  /* per connection – trimmed to save RAM          */
#define MAX_CHARS        20  /* per connection – trimmed to save RAM          */
#define READ_MAX         512
#define LINE_MAX         384

/* ── UART / USB ───────────────────────────────────────────────────────────── */
static const struct device *uart_dev;
static uint8_t  rx_buf[LINE_MAX];
static size_t   rx_len;

/* ── Per-connection slot ──────────────────────────────────────────────────── */
struct svc_info {
    char     uuid[BT_UUID_STR_LEN];
    uint16_t start_handle;
    uint16_t end_handle;
};

struct chr_info {
    char     svc_uuid[BT_UUID_STR_LEN];
    char     uuid[BT_UUID_STR_LEN];
    uint16_t value_handle;
    uint8_t  properties;
};

struct conn_slot {
    struct bt_conn *conn;
    char            mac_str[BT_ADDR_LE_STR_LEN]; /* normalised, lower-case  */
    bool            active;
    struct k_sem    sem;       /* signals connect / disconnect completion      */
    uint8_t         last_err;

    struct svc_info svcs[MAX_SERVICES];
    size_t          svc_count;
    struct chr_info chrs[MAX_CHARS];
    size_t          chr_count;
};

static struct conn_slot slots[MAX_CONNECTIONS];

/* Single shared semaphore for GATT ops (commands are serialised by UART ISR) */
static K_SEM_DEFINE(gatt_sem, 0, 1);

/* Shared read buffer */
static uint8_t read_buf[READ_MAX];
static size_t  read_len;

/* ── Helpers ──────────────────────────────────────────────────────────────── */
static void tx_line(const char *s)           { printk("%s\n", s); }
static void tx_rsp_err(int id, const char *m){ printk("RSP,%d,ERR,%s\n", id, m ? m : ""); }
static void tx_rsp_ok(int id)                { printk("RSP,%d,OK\n", id); }
static void tx_rsp_ok_tag(int id, const char *t){ printk("RSP,%d,OK,%s\n", id, t ? t : ""); }

static void drain_sem(struct k_sem *s)
{
    while (k_sem_take(s, K_NO_WAIT) == 0) {}
}

/* Normalise a MAC string to upper-case for comparison */
static void mac_normalise(const char *in, char *out, size_t out_len)
{
    size_t i;
    for (i = 0; i < out_len - 1 && in[i]; i++) {
        char c = in[i];
        if (c >= 'a' && c <= 'f') c -= 32;
        out[i] = c;
    }
    out[i] = '\0';
}

/* Return true if the MAC portion of a BT addr string matches the given MAC.
 * bt_addr_le_to_str() produces "XX:XX:XX:XX:XX:XX (type)" – we compare only
 * the first 17 characters. */
static bool mac_matches(const char *slot_mac, const char *query_mac)
{
    char a[18], b[18];
    /* copy exactly 17 chars (the XX:XX:XX:XX:XX:XX part) */
    strncpy(a, slot_mac,  17); a[17] = '\0';
    strncpy(b, query_mac, 17); b[17] = '\0';
    mac_normalise(a, a, sizeof(a));
    mac_normalise(b, b, sizeof(b));
    return strcmp(a, b) == 0;
}

static struct conn_slot *slot_by_mac(const char *mac)
{
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (slots[i].active && mac_matches(slots[i].mac_str, mac)) {
            return &slots[i];
        }
    }
    return NULL;
}

static struct conn_slot *slot_free(void)
{
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (!slots[i].active) return &slots[i];
    }
    return NULL;
}

static struct conn_slot *slot_by_conn(struct bt_conn *conn)
{
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        if (slots[i].active && slots[i].conn == conn) {
            return &slots[i];
        }
    }
    return NULL;
}

/* ── ADV queue / thread ───────────────────────────────────────────────────── */
struct adv_msg {
    uint64_t timestamp;
    char     mac_str[BT_ADDR_LE_STR_LEN];
    int8_t   rssi;
    uint8_t  len;
};

K_MSGQ_DEFINE(adv_msgq, sizeof(struct adv_msg), 100, 4);

void usb_adv_thread(void)
{
    struct adv_msg msg;
    char line[128];
    while (1) {
        k_msgq_get(&adv_msgq, &msg, K_FOREVER);
        uint64_t s   = msg.timestamp / 1000ULL;
        uint32_t rem = (uint32_t)(msg.timestamp % 1000ULL);
        snprintk(line, sizeof(line), "ADV,%llu.%03u,%s,%d,%u",
                 (unsigned long long)s, rem, msg.mac_str, msg.rssi, msg.len);
        tx_line(line);
    }
}
K_THREAD_DEFINE(usb_adv_tid, 1024, usb_adv_thread, NULL, NULL, NULL, 7, 0, 0);

/* ── Scan ─────────────────────────────────────────────────────────────────── */
static struct bt_le_scan_param g_scan_param = {
    .type    = BT_HCI_LE_SCAN_PASSIVE,
    .options = BT_LE_SCAN_OPT_NONE,
    .interval = 0x00A0,
    .window   = 0x009F,
};

static void scan_cb(const bt_addr_le_t *addr, int8_t rssi,
                    uint8_t adv_type, struct net_buf_simple *buf)
{
    ARG_UNUSED(adv_type);
    struct adv_msg msg;
    msg.timestamp = k_uptime_get();
    msg.rssi      = rssi;
    msg.len       = buf->len;
    bt_addr_le_to_str(addr, msg.mac_str, sizeof(msg.mac_str));
    k_msgq_put(&adv_msgq, &msg, K_NO_WAIT);
}

static void restart_scan(void)
{
    /* Ignore error – already scanning is fine */
    (void)bt_le_scan_start(&g_scan_param, scan_cb);
}

/* ── Connection callbacks ─────────────────────────────────────────────────── */
static void connected_cb(struct bt_conn *conn, uint8_t err)
{
    struct conn_slot *s = slot_by_conn(conn);

    if (!s) {
        /* Unexpected connection – find a free slot */
        s = slot_free();
        if (s) {
            s->conn   = bt_conn_ref(conn);
            s->active = true;
            bt_addr_le_to_str(bt_conn_get_dst(conn), s->mac_str, sizeof(s->mac_str));
        }
    }

    if (s) {
        s->last_err = err;
        if (err) {
            bt_conn_unref(s->conn);
            s->conn   = NULL;
            s->active = false;
        } else {
            printk("EVENT,CONNECT,%s\n", s->mac_str);
        }
        k_sem_give(&s->sem);
    }

    restart_scan();
}

static void disconnected_cb(struct bt_conn *conn, uint8_t reason)
{
    ARG_UNUSED(reason);
    struct conn_slot *s = slot_by_conn(conn);
    if (!s) return;

    printk("EVENT,DISCONNECT,%s\n", s->mac_str);

    bt_conn_unref(s->conn);
    s->conn      = NULL;
    s->active    = false;
    s->svc_count = 0;
    s->chr_count = 0;
    memset(s->mac_str, 0, sizeof(s->mac_str));

    k_sem_give(&s->sem);
    restart_scan();
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
    .connected    = connected_cb,
    .disconnected = disconnected_cb,
};

/* ── UUID helpers ─────────────────────────────────────────────────────────── */
static bool uuid_matches(const char *a, const char *b)
{
    if (!a || !b) return false;
    while (*a && *b) {
        char ca = *a, cb = *b;
        if (ca >= 'a' && ca <= 'f') ca -= 32;
        if (cb >= 'a' && cb <= 'f') cb -= 32;
        if (ca != cb) return false;
        a++; b++;
    }
    return *a == '\0' && *b == '\0';
}

static struct chr_info *find_char(struct conn_slot *s, const char *uuid)
{
    for (size_t i = 0; i < s->chr_count; i++) {
        if (uuid_matches(s->chrs[i].uuid, uuid)) return &s->chrs[i];
    }
    return NULL;
}

static const char *props_to_flags(uint8_t props)
{
    static char out[96];
    size_t n = 0;
    out[0] = '\0';
    if (props & BT_GATT_CHRC_READ)               n += snprintk(out+n, sizeof(out)-n, "%sread",                   n?"|":"");
    if (props & BT_GATT_CHRC_WRITE)              n += snprintk(out+n, sizeof(out)-n, "%swrite",                  n?"|":"");
    if (props & BT_GATT_CHRC_WRITE_WITHOUT_RESP) n += snprintk(out+n, sizeof(out)-n, "%swrite_without_response", n?"|":"");
    if (props & BT_GATT_CHRC_NOTIFY)             n += snprintk(out+n, sizeof(out)-n, "%snotify",                 n?"|":"");
    if (props & BT_GATT_CHRC_INDICATE)           n += snprintk(out+n, sizeof(out)-n, "%sindicate",               n?"|":"");
    return out;
}

/* ── GATT discovery ───────────────────────────────────────────────────────── */
static struct {
    struct bt_gatt_discover_params params;
    char   current_svc_uuid[BT_UUID_STR_LEN];
    int    req_id;
    struct conn_slot *slot;
} disc_ctx;

static uint8_t discover_chars_cb(struct bt_conn *conn,
                                  const struct bt_gatt_attr *attr,
                                  struct bt_gatt_discover_params *params)
{
    ARG_UNUSED(conn);
    ARG_UNUSED(params);
    if (!attr) { k_sem_give(&gatt_sem); return BT_GATT_ITER_STOP; }

    struct conn_slot *s  = disc_ctx.slot;
    const struct bt_gatt_chrc *chrc = attr->user_data;

    if (s->chr_count < MAX_CHARS) {
        struct chr_info *ci = &s->chrs[s->chr_count++];
        strncpy(ci->svc_uuid, disc_ctx.current_svc_uuid, sizeof(ci->svc_uuid));
        ci->svc_uuid[sizeof(ci->svc_uuid)-1] = '\0';
        bt_uuid_to_str(chrc->uuid, ci->uuid, sizeof(ci->uuid));
        ci->value_handle = chrc->value_handle;
        ci->properties   = chrc->properties;
        printk("CHR,%d,%s,%s,%s\n", disc_ctx.req_id,
               ci->svc_uuid, ci->uuid, props_to_flags(ci->properties));
    }
    return BT_GATT_ITER_CONTINUE;
}

static uint8_t discover_services_cb(struct bt_conn *conn,
                                     const struct bt_gatt_attr *attr,
                                     struct bt_gatt_discover_params *params)
{
    ARG_UNUSED(conn);
    ARG_UNUSED(params);
    if (!attr) { k_sem_give(&gatt_sem); return BT_GATT_ITER_STOP; }

    struct conn_slot *s = disc_ctx.slot;
    const struct bt_gatt_service_val *svc = attr->user_data;

    if (s->svc_count < MAX_SERVICES) {
        bt_uuid_to_str(svc->uuid, s->svcs[s->svc_count].uuid,
                       sizeof(s->svcs[s->svc_count].uuid));
        s->svcs[s->svc_count].start_handle = attr->handle + 1;
        s->svcs[s->svc_count].end_handle   = svc->end_handle;
        s->svc_count++;
    }
    return BT_GATT_ITER_CONTINUE;
}

static int do_discover(int req_id, struct conn_slot *s)
{
    s->svc_count = 0;
    s->chr_count = 0;

    /* Reduce USB-serial spam and BLE controller load during discovery. */
    k_msgq_purge(&adv_msgq);
    (void)bt_le_scan_stop();

    tx_rsp_ok_tag(req_id, "DISCOVER_BEGIN");

    memset(&disc_ctx, 0, sizeof(disc_ctx));
    disc_ctx.req_id = req_id;
    disc_ctx.slot   = s;
    disc_ctx.params.type         = BT_GATT_DISCOVER_PRIMARY;
    disc_ctx.params.start_handle = BT_ATT_FIRST_ATTRIBUTE_HANDLE;
    disc_ctx.params.end_handle   = BT_ATT_LAST_ATTRIBUTE_HANDLE;
    disc_ctx.params.func         = discover_services_cb;

    drain_sem(&gatt_sem);
    int err = bt_gatt_discover(s->conn, &disc_ctx.params);
    if (err) return err;
    if (k_sem_take(&gatt_sem, K_SECONDS(15)) != 0) {
        tx_rsp_err(req_id, "Discover timeout (services)");
        restart_scan();
        return -ETIMEDOUT;
    }

    for (size_t i = 0; i < s->svc_count; i++) {
        printk("SVC,%d,%s\n", req_id, s->svcs[i].uuid);
    }

    for (size_t i = 0; i < s->svc_count; i++) {
        memset(&disc_ctx, 0, sizeof(disc_ctx));
        disc_ctx.req_id = req_id;
        disc_ctx.slot   = s;
        strncpy(disc_ctx.current_svc_uuid, s->svcs[i].uuid,
                sizeof(disc_ctx.current_svc_uuid));
        disc_ctx.current_svc_uuid[sizeof(disc_ctx.current_svc_uuid)-1] = '\0';

        disc_ctx.params.type         = BT_GATT_DISCOVER_CHARACTERISTIC;
        disc_ctx.params.start_handle = s->svcs[i].start_handle;
        disc_ctx.params.end_handle   = s->svcs[i].end_handle;
        disc_ctx.params.func         = discover_chars_cb;

        drain_sem(&gatt_sem);
        err = bt_gatt_discover(s->conn, &disc_ctx.params);
        if (err) return err;
        if (k_sem_take(&gatt_sem, K_SECONDS(10)) != 0) {
            tx_rsp_err(req_id, "Discover timeout (characteristics)");
            restart_scan();
            return -ETIMEDOUT;
        }
    }

    tx_rsp_ok_tag(req_id, "DISCOVER_END");
    restart_scan();
    return 0;
}

/* ── GATT read ────────────────────────────────────────────────────────────── */
static uint8_t read_cb(struct bt_conn *conn, uint8_t err,
                        struct bt_gatt_read_params *params,
                        const void *data, uint16_t length)
{
    ARG_UNUSED(conn); ARG_UNUSED(params);
    if (err || !data || length == 0) { k_sem_give(&gatt_sem); return BT_GATT_ITER_STOP; }

    size_t copy = length;
    if (read_len + copy > sizeof(read_buf)) copy = sizeof(read_buf) - read_len;
    memcpy(read_buf + read_len, data, copy);
    read_len += copy;
    return BT_GATT_ITER_CONTINUE;
}

static int do_read(int req_id, struct conn_slot *s, const char *char_uuid)
{
    struct chr_info *ci = find_char(s, char_uuid);
    if (!ci) return -ENOENT;

    read_len = 0;
    static struct bt_gatt_read_params rp;
    memset(&rp, 0, sizeof(rp));
    rp.func                = read_cb;
    rp.handle_count        = 1;
    rp.single.handle       = ci->value_handle;
    rp.single.offset       = 0;

    drain_sem(&gatt_sem);
    int err = bt_gatt_read(s->conn, &rp);
    if (err) return err;
    k_sem_take(&gatt_sem, K_SECONDS(10));

    static char hex_out[READ_MAX * 2 + 1];
    for (size_t i = 0; i < read_len; i++) {
        snprintk(hex_out + (i*2), sizeof(hex_out) - (i*2), "%02x", read_buf[i]);
    }
    hex_out[read_len * 2] = '\0';
    printk("RSP,%d,OK,READ,%s\n", req_id, hex_out);
    return 0;
}

/* ── GATT write ───────────────────────────────────────────────────────────── */
static void write_cb(struct bt_conn *conn, uint8_t err,
                     struct bt_gatt_write_params *params)
{
    ARG_UNUSED(conn); ARG_UNUSED(params); ARG_UNUSED(err);
    k_sem_give(&gatt_sem);
}

static int hex_to_bytes(const char *hex, uint8_t *out, size_t out_max, size_t *out_len)
{
    size_t len = strlen(hex);
    if (len % 2 != 0) return -EINVAL;
    size_t n = len / 2;
    if (n > out_max) return -ENOMEM;
    for (size_t i = 0; i < n; i++) {
        char bs[3] = { hex[i*2], hex[i*2+1], 0 };
        unsigned int v;
        if (sscanf(bs, "%x", &v) != 1) return -EINVAL;
        out[i] = (uint8_t)v;
    }
    *out_len = n;
    return 0;
}

static int do_write(int req_id, struct conn_slot *s,
                    const char *char_uuid, const char *hex)
{
    struct chr_info *ci = find_char(s, char_uuid);
    if (!ci) return -ENOENT;

    static uint8_t value[READ_MAX];
    size_t value_len = 0;
    int err = hex_to_bytes(hex, value, sizeof(value), &value_len);
    if (err) return err;

    static struct bt_gatt_write_params wp;
    memset(&wp, 0, sizeof(wp));
    wp.handle = ci->value_handle;
    wp.offset = 0;
    wp.data   = value;
    wp.length = value_len;
    wp.func   = write_cb;

    drain_sem(&gatt_sem);
    err = bt_gatt_write(s->conn, &wp);
    if (err) return err;
    k_sem_take(&gatt_sem, K_SECONDS(10));

    tx_rsp_ok(req_id);
    return 0;
}

/* ── Connect / Disconnect ─────────────────────────────────────────────────── */
static int do_connect(int req_id, const char *mac)
{
    /* Already connected to this MAC? */
    if (slot_by_mac(mac)) {
        tx_rsp_err(req_id, "Already connected");
        return -EALREADY;
    }

    struct conn_slot *s = slot_free();
    if (!s) {
        tx_rsp_err(req_id, "No free slots");
        return -ENOMEM;
    }

    bt_addr_le_t addr;
    int err = bt_addr_le_from_str(mac, "random", &addr);
    if (err) err = bt_addr_le_from_str(mac, "public", &addr);
    if (err) {
        tx_rsp_err(req_id, "Invalid MAC");
        return -EINVAL;
    }

    /* Pre-fill slot so connected_cb can find it */
    s->active   = true;
    s->last_err = 0;
    bt_addr_le_to_str(&addr, s->mac_str, sizeof(s->mac_str));
    drain_sem(&s->sem);

    bt_le_scan_stop();

    struct bt_conn_le_create_param cp = {
        .options        = BT_CONN_LE_OPT_NONE,
        .interval       = BT_GAP_INIT_CONN_INT_MIN,
        .window         = BT_GAP_INIT_CONN_INT_MIN,
        .interval_coded = 0,
        .window_coded   = 0,
        .timeout        = 0,
    };
    struct bt_le_conn_param lp = {
        .interval_min = BT_GAP_INIT_CONN_INT_MIN,
        .interval_max = BT_GAP_INIT_CONN_INT_MAX,
        .latency      = 0,
        .timeout      = 400,
    };

    err = bt_conn_le_create(&addr, &cp, &lp, &s->conn);
    if (err) {
        s->active = false;
        memset(s->mac_str, 0, sizeof(s->mac_str));
        tx_rsp_err(req_id, "bt_conn_le_create failed");
        restart_scan();
        return err;
    }

    k_sem_take(&s->sem, K_SECONDS(15));

    if (s->last_err != 0 || !s->conn) {
        s->active = false;
        memset(s->mac_str, 0, sizeof(s->mac_str));
        tx_rsp_err(req_id, "Connect failed");
        restart_scan();
        return -EIO;
    }

    tx_rsp_ok(req_id);
    restart_scan();
    return 0;
}

static int do_disconnect(int req_id, const char *mac)
{
    struct conn_slot *s = slot_by_mac(mac);
    if (!s) {
        tx_rsp_err(req_id, "Not connected");
        return -ENOENT;
    }

    drain_sem(&s->sem);
    int err = bt_conn_disconnect(s->conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
    if (err) {
        tx_rsp_err(req_id, "Disconnect failed");
        return err;
    }

    /* Wait for disconnected_cb to fire (it unrefs and clears the slot) */
    k_sem_take(&s->sem, K_SECONDS(5));
    tx_rsp_ok(req_id);
    return 0;
}

/* ── Command parser ───────────────────────────────────────────────────────── */
static void handle_command(char *line)
{
    char *sp = NULL;
    char *tok = strtok_r(line, ",", &sp);
    if (!tok || strcmp(tok, "REQ") != 0) return;

    char *id_str = strtok_r(NULL, ",", &sp);
    char *cmd    = strtok_r(NULL, ",", &sp);
    if (!id_str || !cmd) return;

    int req_id = atoi(id_str);

    /* ── CONNECT ── */
    if (strcmp(cmd, "CONNECT") == 0) {
        char *mac = strtok_r(NULL, ",", &sp);
        if (!mac) { tx_rsp_err(req_id, "Missing MAC"); return; }
        (void)do_connect(req_id, mac);
        return;
    }

    /* ── DISCONNECT ── */
    if (strcmp(cmd, "DISCONNECT") == 0) {
        char *mac = strtok_r(NULL, ",", &sp);
        if (!mac) { tx_rsp_err(req_id, "Missing MAC"); return; }
        (void)do_disconnect(req_id, mac);
        return;
    }

    /* ── LIST (show active connections) ── */
    if (strcmp(cmd, "LIST") == 0) {
        int found = 0;
        for (int i = 0; i < MAX_CONNECTIONS; i++) {
            if (slots[i].active) {
                printk("CONN,%d,%d,%s\n", req_id, i, slots[i].mac_str);
                found++;
            }
        }
        if (found == 0) printk("CONN,%d,NONE\n", req_id);
        tx_rsp_ok(req_id);
        return;
    }

    /* ── All remaining commands need a MAC ── */
    char *mac = strtok_r(NULL, ",", &sp);
    if (!mac) { tx_rsp_err(req_id, "Missing MAC"); return; }

    struct conn_slot *s = slot_by_mac(mac);
    if (!s) { tx_rsp_err(req_id, "Not connected"); return; }

    /* ── DISCOVER ── */
    if (strcmp(cmd, "DISCOVER") == 0) {
        int err = do_discover(req_id, s);
        if (err) tx_rsp_err(req_id, "Discover failed");
        return;
    }

    /* ── READ ── */
    if (strcmp(cmd, "READ") == 0) {
        char *uuid = strtok_r(NULL, ",", &sp);
        if (!uuid) { tx_rsp_err(req_id, "Missing UUID"); return; }
        int err = do_read(req_id, s, uuid);
        if      (err == -ENOENT) tx_rsp_err(req_id, "UUID not found; run DISCOVER first");
        else if (err)            tx_rsp_err(req_id, "Read failed");
        return;
    }

    /* ── WRITE ── */
    if (strcmp(cmd, "WRITE") == 0) {
        char *uuid = strtok_r(NULL, ",", &sp);
        char *hex  = strtok_r(NULL, ",", &sp);
        if (!uuid || !hex) { tx_rsp_err(req_id, "Missing UUID/HEX"); return; }
        int err = do_write(req_id, s, uuid, hex);
        if      (err == -ENOENT) tx_rsp_err(req_id, "UUID not found; run DISCOVER first");
        else if (err)            tx_rsp_err(req_id, "Write failed");
        return;
    }

    tx_rsp_err(req_id, "Unknown CMD");
}

/* ── UART ISR ─────────────────────────────────────────────────────────────── */
static void uart_cb(const struct device *dev, void *user_data)
{
    ARG_UNUSED(user_data);
    if (!uart_irq_update(dev) || !uart_irq_rx_ready(dev)) return;

    uint8_t c;
    while (uart_fifo_read(dev, &c, 1) == 1) {
        if (c == '\n' || c == '\r') {
            if (rx_len > 0) {
                rx_buf[rx_len] = '\0';
                handle_command((char *)rx_buf);
                rx_len = 0;
            }
            continue;
        }
        if (rx_len < LINE_MAX - 1) rx_buf[rx_len++] = c;
        else                        rx_len = 0;
    }
}

/* ── main ─────────────────────────────────────────────────────────────────── */
int main(void)
{
    /* Initialise per-slot semaphores */
    for (int i = 0; i < MAX_CONNECTIONS; i++) {
        k_sem_init(&slots[i].sem, 0, 1);
    }

    uart_dev = DEVICE_DT_GET_ONE(zephyr_cdc_acm_uart);
    if (!device_is_ready(uart_dev)) return 0;

    if (usb_enable(NULL)) return 0;

    uint32_t dtr = 0;
    while (!dtr) {
        uart_line_ctrl_get(uart_dev, UART_LINE_CTRL_DTR, &dtr);
        k_sleep(K_MSEC(100));
    }

    uart_irq_callback_user_data_set(uart_dev, uart_cb, NULL);
    uart_irq_rx_enable(uart_dev);

    if (bt_enable(NULL)) {
        tx_line("ERR,Bluetooth init failed");
        return 0;
    }

    if (bt_le_scan_start(&g_scan_param, scan_cb)) {
        tx_line("ERR,Starting scanning failed");
        return 0;
    }

    while (1) k_sleep(K_FOREVER);
    return 0;
}