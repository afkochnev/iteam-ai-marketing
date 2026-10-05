"""Acceptance-only provider guard, mounted into every Python runtime process."""

import os

if os.environ.get("ACCEPTANCE_DENY_PROVIDERS") == "1":
    import httpx
    import redis

    def deny_http(*args, **kwargs):
        # Mock/ASGI transports used by pytest never reach a provider network.
        transport = getattr(args[0], "_transport", None) if args else None
        if transport is not None and not isinstance(
            transport, httpx.HTTPTransport | httpx.AsyncHTTPTransport
        ):
            return None
        client = redis.Redis.from_url(os.environ["REDIS_URL"])
        client.incr("acceptance:provider_calls")
        raise RuntimeError(
            "Provider HTTP calls are forbidden in deterministic acceptance"
        )

    original_send = httpx.Client.send
    original_async_send = httpx.AsyncClient.send

    def guarded_send(*args, **kwargs):
        deny_http(*args, **kwargs)
        return original_send(*args, **kwargs)

    async def guarded_async_send(*args, **kwargs):
        deny_http(*args, **kwargs)
        return await original_async_send(*args, **kwargs)

    httpx.Client.send = guarded_send
    httpx.AsyncClient.send = guarded_async_send
