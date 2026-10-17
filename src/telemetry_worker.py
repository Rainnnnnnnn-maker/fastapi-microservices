from workers import asgi

from ems.telemetry.app import create_app

Default = asgi.entrypoint(create_app())
