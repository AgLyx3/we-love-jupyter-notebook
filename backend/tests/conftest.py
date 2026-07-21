import json

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app


@pytest.fixture
def notebook_payload():
    def build(*, cell_ids=("intro", "editable")) -> bytes:
        cells = [
            {
                "cell_type": "markdown",
                "id": cell_ids[0],
                "metadata": {},
                "source": ["# Example notebook\n"],
            },
            {
                "cell_type": "code",
                "id": cell_ids[1],
                "metadata": {},
                "source": ["value = 1\n"],
                "execution_count": None,
                "outputs": [],
            },
        ]
        return json.dumps(
            {
                "cells": cells,
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ).encode()

    return build


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client
