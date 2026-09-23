"""
Milestone 1 self-test: prove the Python runtime and every dependency are usable.

If any of these fail, nothing else in the engine can be trusted to run.
"""
import importlib
import sys

import pytest

REQUIRED_PACKAGES = ["websockets", "aiohttp", "pydantic", "pytest", "pytest_asyncio", "cryptography"]


def test_python_version_is_3_11_or_newer():
    assert sys.version_info >= (3, 11), f"Python 3.11+ required, found {sys.version}"


@pytest.mark.parametrize("name", REQUIRED_PACKAGES)
def test_package_imports(name):
    module = importlib.import_module(name)
    assert module is not None


def test_websockets_modern_asyncio_api_available():
    # The feed is written against websockets' modern asyncio API (v13+).
    from websockets.asyncio.client import connect  # noqa: F401
    from websockets.asyncio.server import serve  # noqa: F401


def test_pydantic_is_v2():
    import pydantic

    assert int(pydantic.VERSION.split(".")[0]) >= 2


async def test_event_loop_runs_coroutines():
    import asyncio

    await asyncio.sleep(0)
    assert True


def test_cryptography_rsa_backend_loads():
    # The OS-bundled cryptography 41 panics on import under Python 3.11; requirements pin >= 42.
    from cryptography.hazmat.primitives.asymmetric import rsa

    assert rsa.generate_private_key(public_exponent=65537, key_size=2048).key_size == 2048
