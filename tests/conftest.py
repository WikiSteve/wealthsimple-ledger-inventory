import pytest


@pytest.fixture(scope="module")
def fixture_browser():
    """An isolated headless browser; never attach tests to a signed-in profile."""
    import shutil
    selenium = pytest.importorskip("selenium.webdriver")
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    chromium = shutil.which("chromium")
    chromedriver = shutil.which("chromedriver")
    if not chromium or not chromedriver:
        pytest.skip("Chromium and ChromeDriver required for export DOM regression")
    options = Options()
    options.binary_location = chromium
    options.add_argument("--headless")
    driver = selenium.Chrome(service=Service(chromedriver), options=options)
    yield driver
    driver.quit()
