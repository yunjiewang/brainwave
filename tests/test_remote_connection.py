import http.server
import socketserver
import socket

PORT = 3005

class MyHandler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/html')
        self.end_headers()
        message = f"""
        <html>
        <body>
        <h1>Test Server Running!</h1>
        <p>This server is running on {socket.gethostbyname(socket.gethostname())}:{PORT}</p>
        <p>Your connection is coming from: {self.client_address[0]}</p>
        <p>Path requested: {self.path}</p>
        </body>
        </html>
        """
        self.wfile.write(message.encode())

with socketserver.TCPServer(("0.0.0.0", PORT), MyHandler) as httpd:
    print(f"Server running at http://0.0.0.0:{PORT}")
    print(f"Local network IP: {socket.gethostbyname(socket.gethostname())}")
    httpd.serve_forever()