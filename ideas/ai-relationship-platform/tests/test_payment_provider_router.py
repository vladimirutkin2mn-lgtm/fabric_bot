from typing import cast

import pytest

from app.providers.payments.base import PaymentProvider, PaymentProviderError, PaymentProviderName
from app.providers.payments.router import PaymentProviderRouter
from tests.test_billing_config import production


def test_mock_rejected_in_production() -> None:
    router = PaymentProviderRouter(
        production(), {PaymentProviderName.MOCK: cast(PaymentProvider, object())}
    )
    with pytest.raises(PaymentProviderError):
        router.get(PaymentProviderName.MOCK)
