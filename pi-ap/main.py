import argparse
import asyncio
import logging
import grpc
from concurrent import futures

from grpc_proto import access_point_pb2_grpc
from grpc_server import AccessPointServiceServicer

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

async def serve(ip: str, port: int, ca_cert: str, server_cert: str, server_key: str):
    server = grpc.aio.server(futures.ThreadPoolExecutor(max_workers=10))
    
    servicer = AccessPointServiceServicer(ap_ip=ip)
    access_point_pb2_grpc.add_AccessPointServiceServicer_to_server(servicer, server)

    # mTLS credentials
    with open(server_key, 'rb') as f:
        private_key = f.read()
    with open(server_cert, 'rb') as f:
        certificate_chain = f.read()
    with open(ca_cert, 'rb') as f:
        root_certificates = f.read()

    server_credentials = grpc.ssl_server_credentials(
        [(private_key, certificate_chain)],
        root_certificates=root_certificates,
        require_client_auth=True
    )

    listen_addr = f"0.0.0.0:{port}"
    server.add_secure_port(listen_addr, server_credentials)
    
    logger.info(f"Starting gRPC server on {listen_addr} with mTLS...")
    await server.start()
    await server.wait_for_termination()

def main():
    parser = argparse.ArgumentParser(description="TieDie Pi AP gRPC Server")
    parser.add_argument("--ip", required=True, help="IP address of this Pi (used as AP identity)")
    parser.add_argument("--port", type=int, default=50051, help="Port to listen on (default 50051)")
    parser.add_argument("--ca-cert", required=True, help="Path to CA certificate (ca.crt)")
    parser.add_argument("--server-cert", required=True, help="Path to server certificate (server.crt)")
    parser.add_argument("--server-key", required=True, help="Path to server private key (server.key)")

    args = parser.parse_args()

    asyncio.run(serve(args.ip, args.port, args.ca_cert, args.server_cert, args.server_key))

if __name__ == "__main__":
    main()
