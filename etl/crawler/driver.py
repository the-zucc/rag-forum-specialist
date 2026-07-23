import logging
import os
import platform
import shutil

logger = logging.getLogger(__name__)

ARM_MACHINES = ("aarch64", "arm64")

CHROMIUM_CANDIDATES = ("chromium", "chromium-browser")


def resolve_uc(uc_mode="auto") -> bool:
    """Decide whether to run in UC (undetected-chromedriver) mode.

    SeleniumBase's uc_driver binary is published for x86_64 only, so "auto"
    disables UC mode on ARM (e.g. Apple Silicon / ARM servers) and enables it
    everywhere else.
    """
    if isinstance(uc_mode, bool):
        return uc_mode
    mode = str(uc_mode).strip().lower()
    if mode in ("on", "true", "1", "yes"):
        return True
    if mode in ("off", "false", "0", "no"):
        return False
    return platform.machine().lower() not in ARM_MACHINES


def find_browser_binary():
    """Return an explicit Chromium binary path when Google Chrome is absent.

    Selenium only auto-discovers google-chrome; images like
    selenium/standalone-chromium ship a plain `chromium` binary instead.
    """
    if shutil.which("google-chrome"):
        return None  # selenium finds this on its own
    for name in CHROMIUM_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return os.getenv("CHROME_BIN")


def _build_plain_chromium(headless: bool):
    """Vanilla Selenium Chromium with explicit binary/driver paths.

    selenium-manager has no linux/aarch64 build, so on ARM every path must
    be supplied by hand: the system chromedriver and the chromium binary.
    """
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service

    options = Options()
    options.page_load_strategy = os.getenv("PAGE_LOAD_STRATEGY", "normal")
    binary = find_browser_binary()
    if binary:
        options.binary_location = binary
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")

    driver_path = os.getenv("CHROMEDRIVER_BIN") or shutil.which("chromedriver")
    service = Service(executable_path=driver_path) if driver_path else Service()
    return webdriver.Chrome(service=service, options=options)


def build_driver(headless: bool = True, uc_mode="auto"):
    """Create a Chromium driver.

    UC mode (SeleniumBase undetected-chromedriver) spoofs the automation
    fingerprints Cloudflare/PerimeterX-style bot checks look for; it works
    best headed or under Xvfb (as in the selenium/standalone-chromium base
    image). On ARM there is no uc_driver or selenium-manager binary, so a
    regular Chromium session with the system chromedriver is used instead.

    Returns (driver, uc) where uc says which mode was actually used.
    """
    uc = resolve_uc(uc_mode)
    logger.info(
        "Launching Chromium (machine=%s, uc=%s, headless=%s)",
        platform.machine(), uc, headless,
    )
    if uc:
        from seleniumbase import Driver

        return Driver(uc=True, headless=headless, binary_location=find_browser_binary()), True
    return _build_plain_chromium(headless), False
