"""Запуск SOCKS5-прокси через Tor с помощью torpy.

Слушает 127.0.0.1:10808 и маршрутизирует трафик через сеть Tor.
"""

import asyncio
import socket
import struct
import threading

from loguru import logger

from torpy import TorClient
from torpy.circuit import TorCircuit


def handle_client(client_socket: socket.socket, tor_circuit: TorCircuit):
    """Обрабатываем SOCKS5-соединение от клиента."""
    try:
        # 1. Приветствие SOCKS5
        data = client_socket.recv(2)
        if len(data) < 2 or data[0] != 0x05:
            client_socket.close()
            return
        nmethods = data[1]
        client_socket.recv(nmethods)
        client_socket.sendall(b'\x05\x00')  # no auth

        # 2. Запрос
        data = client_socket.recv(4)
        if len(data) < 4 or data[0] != 0x05:
            client_socket.close()
            return
        cmd = data[1]
        atyp = data[3]

        if atyp == 0x01:  # IPv4
            addr = socket.inet_ntoa(client_socket.recv(4))
        elif atyp == 0x03:  # Domain
            alen = client_socket.recv(1)[0]
            addr = client_socket.recv(alen).decode()
        else:
            client_socket.sendall(b'\x05\x08\x00\x01\x00\x00\x00\x00\x00\x00')
            client_socket.close()
            return

        port = struct.unpack('!H', client_socket.recv(2))[0]

        if cmd != 0x01:  # CONNECT
            client_socket.sendall(b'\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00')
            client_socket.close()
            return

        # 3. Подключаемся через Tor
        logger.info(f"SOCKS5: {addr}:{port}")
        stream = tor_circuit.create_stream((addr, port))
        client_socket.sendall(b'\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00')

        # 4. Обмен данными
        def forward(src, dst):
            try:
                while True:
                    data = src.recv(4096)
                    if not data:
                        break
                    dst.sendall(data)
            except Exception:
                pass
            finally:
                try:
                    src.close()
                except Exception:
                    pass
                try:
                    dst.close()
                except Exception:
                    pass

        t1 = threading.Thread(target=forward, args=(client_socket, stream))
        t2 = threading.Thread(target=forward, args=(stream, client_socket))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
    except Exception as e:
        logger.error(f"SOCKS5 error: {e}")
        try:
            client_socket.close()
        except Exception:
            pass


def main():
    logger.info("Запускаем Tor SOCKS5 прокси на 127.0.0.1:10808...")

    with TorClient() as tor:
        logger.info("Tor клиент инициализирован")
        with tor.create_circuit() as circuit:
            logger.info("Tor circuit создан")

            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', 10808))
            server.listen(128)
            logger.info("SOCKS5 прокси слушает 127.0.0.1:10808")

            try:
                while True:
                    client, _ = server.accept()
                    threading.Thread(
                        target=handle_client,
                        args=(client, circuit),
                        daemon=True
                    ).start()
            except KeyboardInterrupt:
                logger.info("Остановка прокси...")
            finally:
                server.close()


if __name__ == "__main__":
    main()
