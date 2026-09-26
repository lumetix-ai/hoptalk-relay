import pytest
from django.test import Client

from tests.panel.panel_client import sign_in_test_client
from tests.panel_operator import PanelOperator


@pytest.fixture
def signed_in_client(panel_operator: PanelOperator) -> Client:
    return sign_in_test_client(panel_operator)
