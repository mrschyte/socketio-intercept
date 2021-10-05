from asgiref.wsgi import WsgiToAsgi
from flask import Flask, request, make_response
from dataclasses import dataclass

import aiohttp
import typing
import socketio
import uuid
import json
import asyncio
import uvicorn
import click
import httpx
import base64


@dataclass
class Session:
    client: socketio.AsyncClient
    lock: asyncio.Lock
    events: [typing.List[typing.Any]]


json_encoder_default = json.JSONEncoder.default


def json_bytes_encoder_default(self, o):
    if type(o) == bytes:
        return {
            'type': '__json_bytes_value__',
            'value': base64.b64encode(o).decode('ascii')
        }
    return json_encoder_default(self, o)


json.JSONEncoder.default = json_bytes_encoder_default


def json_decode_bytes_hook(d):
    if 'type' in d and d['type'] == '__json_bytes_value__' and 'value' in d:
        return base64.b64decode(d['value'])
    return d


class ServerCatchAllNamespace(socketio.AsyncNamespace):
    def __init__(self, namespace, listener, proxies={}, poll_wait=1):
        socketio.AsyncNamespace.__init__(self, namespace)

        # url for the uvicorn server listener
        self.listener = listener

        # maps from socket io sessions to rest sessions
        self.sessions = {}

        # interception proxies to use
        self.proxies = proxies

        # time to sleep between polls
        self.poll_wait = poll_wait

    async def trigger_event(self, *args):
        async def poll(sid):
            async with httpx.AsyncClient(proxies=self.proxies) as client:
                while sid in self.sessions:
                    response = await client.get('{}/poll/{}'.format(
                        self.listener, self.sessions[sid]
                    ))
                    events = response.json(object_hook=json_decode_bytes_hook)
                    for event in events:
                        method, args = event[0], event[1:]
                        await self.emit(method, *args, room=sid)
                        await asyncio.sleep(0)
                    await asyncio.sleep(self.poll_wait)

        method, sid, args = args[0], args[1], args[2:]

        async with httpx.AsyncClient(proxies=self.proxies) as client:
            if method == 'connect':
                response = await client.get('{}/connect'.format(self.listener))
                self.sessions[sid] = response.text
                asyncio.create_task(poll(sid))
            elif method == 'disconnect':
                await client.get(
                    '{}/disconnect/{}'.format(
                        self.listener, self.sessions[sid])
                )
                del self.sessions[sid]
            else:
                await client.post(
                    '{}/emit/{}'.format(
                        self.listener, self.sessions[sid]),
                    json={'event': method, 'args': args}
                )


class ClientCatchAllNamespace(socketio.AsyncClientNamespace):
    def __init__(self, namespace, session=None):
        socketio.AsyncClientNamespace.__init__(self, namespace)
        self.session = session

    async def trigger_event(self, *args):
        async with self.session.lock:
            self.session.events.append(args)


class RestAPIServer(Flask):
    def __init__(self, sockio_url):
        Flask.__init__(self, __name__)
        self.sockio_url = sockio_url
        self.sessions = {}
        self._init_rules()

    def _init_rules(self):
        self.add_url_rule("/connect", "connect", self.connect)
        self.add_url_rule("/disconnect/<sid>", "disconnect", self.disconnect)
        self.add_url_rule("/emit/<sid>", "emit", self.emit, methods=['POST'])
        self.add_url_rule("/poll/<sid>", "poll", self.poll)

    # @app.route("/connect")
    async def connect(self):
        sid = str(uuid.uuid4())

        # get http_proxy and https_proxy from environment
        http_session = aiohttp.ClientSession(trust_env=True)
        session = Session(
            socketio.AsyncClient(logger=False, engineio_logger=False,
                                 http_session=http_session),
            asyncio.Lock(),
            []
        )
        self.sessions[sid] = session

        session.client.register_namespace(
            ClientCatchAllNamespace('/', session=session))

        await session.client.connect(self.sockio_url)
        return sid

    # @app.route("/disconnect/<sid>")
    async def disconnect(self, sid):
        if sid in self.sessions:
            session = self.sessions[sid]
            await session.client.disconnect()
            del self.sessions[sid]
            return 'OK'
        return 'KO'

    # @app.route("/emit/<sid>", methods=['POST'])
    async def emit(self, sid):
        if sid in self.sessions:
            session = self.sessions[sid]
            data = request.json
            if data is not None and 'event' in data and 'args' in data:
                await session.client.emit(data['event'], *data['args'])
                return 'OK'
        return 'KO'

    # @app.route("/poll/<sid>")
    async def poll(self, sid):
        if sid in self.sessions:
            session = self.sessions[sid]
            async with session.lock:
                resp = make_response(json.dumps(session.events))
                resp.mimetype = 'application/json'
                session.events.clear()
                return resp
        return 'KO'


@click.command()
@click.option('--listen-host', default='localhost', type=str, show_default=True)
@click.option('--listen-port', default=8000, type=int, show_default=True)
@click.option('--mitm-proxy', default='http://localhost:8080', type=str, show_default=True)
@click.option('--sockio-url', default='https://socketio-chat-h9jt.herokuapp.com', type=str, show_default=True)
def main(listen_host, listen_port, mitm_proxy, sockio_url):
    proxies = {
        'http://': mitm_proxy,
        'https://': mitm_proxy
    }

    sock_srv = socketio.AsyncServer(
        async_mode='asgi', logger=False, engineio_logger=False)

    sock_srv.register_namespace(
        ServerCatchAllNamespace('/', 'http://{}:{}'.format(
            listen_host, listen_port), proxies)
    )

    rest_app = WsgiToAsgi(RestAPIServer(sockio_url))
    main_app = socketio.ASGIApp(sock_srv, other_asgi_app=rest_app)

    uvicorn.run(main_app, host=listen_host, port=listen_port)

if __name__ == '__main__':
    main()
