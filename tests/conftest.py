import pytest

from timed_sketching_helper import db as db_module


@pytest.fixture
def conn():
    connection = db_module.connect(":memory:")
    db_module.init_db(connection)
    try:
        yield connection
    finally:
        connection.close()
