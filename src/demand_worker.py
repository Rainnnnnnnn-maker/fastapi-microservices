from workers import asgi

from ems.demand.app import create_app

Default = asgi.entrypoint(create_app())
