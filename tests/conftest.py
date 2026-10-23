import warnings
import pytest

warnings.filterwarnings("ignore")
V1 = "data/ct200_manual_v1.pdf"
V2 = "data/ct200_manual_v2.pdf"


@pytest.fixture(scope="session")
def v1():
    from app.parser import parse_pdf
    return parse_pdf(V1)


@pytest.fixture(scope="session")
def v2():
    from app.parser import parse_pdf
    return parse_pdf(V2)
