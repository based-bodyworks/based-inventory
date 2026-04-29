# Based Inventory Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a production daily bot that replaces the existing "Inventory Brain" (quantity-tier alerts + weekly snapshot) and adds a new ATC audit layer reconciling Shopify inventory truth against rendered Add-to-Cart button state on the live site, deployed on Render Cron posting to Slack `#alerts-inventory`.

**Architecture:** One Python 3.12 repo, three cron entrypoints (`quantity_alerts`, `atc_audit`, `weekly_snapshot`) sharing modules under `src/based_inventory/`. Playwright for rendered-DOM ATC detection. Shopify Admin GraphQL `2026-01` for inventory truth. Slack Block Kit for alerts with Telegram fallback. Persistent state on Render Disk for alert dedup.

**Tech Stack:** Python 3.12, `requests`, `playwright-python`, `pytest`, `ruff`, Docker (base: `mcr.microsoft.com/playwright/python`), Render Cron, GitHub Actions CI.

**Spec:** `docs/specs/2026-04-15-based-inventory-design.md`

---

## Phase 0: Prerequisites (manual, Avi owns, parallel to dev)

These unblock specific later phases. Start them now so they're ready by the time dev needs them. None block Phase 1.

- [ ] **Create Shopify "Based Inventory" custom app in Based Partners org** ([partners.shopify.com/4860762](https://partners.shopify.com/4860762/)). Scopes: `read_products`, `read_inventory`, `read_locations`. Generate custom install link. (Needed by Phase 2 for dev token; by Phase 15 for prod token.)

- [ ] **Install Shopify app on dev store.** Save token as `SHOPIFY_TOKEN_DEV` and store domain as `SHOPIFY_STORE_DEV` in password manager. (Needed by Phase 2.)

- [ ] **Install Shopify app on prod store (`basedbodyworks.com`).** Save token as `SHOPIFY_TOKEN_PROD`. (Needed by Phase 15.)

- [ ] **Create Slack app "Based Inventory" in Based workspace.** Scopes: `chat:write`, `chat:write.public`. Request workspace admin install. Save bot token as `SLACK_BOT_TOKEN`. Display name "Based Inventory". (Needed by Phase 5.)

- [ ] **Create Render account (team billing, not personal).** One Render project "based-inventory" under a team email that survives personnel changes. (Needed by Phase 15.)

- [ ] **Telegram bot chat_id for fallback alerting.** Either reuse Inventory Brain's `telegram_alert_chat_id` (ask colleague) or skip Telegram fallback for v0 (alerts fall back to Render logs only). Recommend reuse. (Needed by Phase 6.)

---

## Phase 1: Project scaffolding

**Files:**
- Create: `.gitignore`
- Create: `README.md`
- Create: `pyproject.toml`
- Create: `requirements.txt`
- Create: `requirements-dev.txt`
- Create: `.env.example`
- Create: `Dockerfile`
- Create: `.github/workflows/ci.yml`
- Create: `src/based_inventory/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/conftest.py`

- [ ] **Step 1.1: Create `.gitignore`**

```gitignore
# Python
__pycache__/
*.py[cod]
*.egg-info/
.venv/
venv/
.pytest_cache/
.ruff_cache/
.mypy_cache/
.coverage
htmlcov/

# Env
.env
.env.local

# State (persisted on Render Disk, never committed)
data/alert-state.json

# IDE
.vscode/
.idea/
.DS_Store

# Playwright
playwright-report/
test-results/
```

- [ ] **Step 1.2: Create `requirements.txt`**

```
requests==2.32.3
playwright==1.44.0
python-dotenv==1.0.1
```

- [ ] **Step 1.3: Create `requirements-dev.txt`**

```
-r requirements.txt
pytest==8.3.3
pytest-mock==3.14.0
ruff==0.6.9
```

- [ ] **Step 1.4: Create `pyproject.toml`**

```toml
[project]
name = "based-inventory"
version = "0.1.0"
description = "Based BodyWorks inventory monitor + ATC audit"
requires-python = ">=3.12"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM", "RUF"]
ignore = ["E501"]

[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
addopts = "-v --tb=short"

[tool.setuptools.packages.find]
where = ["src"]
```

- [ ] **Step 1.5: Create `.env.example`**

```
# Shopify
SHOPIFY_STORE=basedbodyworks.myshopify.com
SHOPIFY_TOKEN=shpat_replace_me
SHOPIFY_API_VERSION=2026-01

# Slack
SLACK_BOT_TOKEN=xoxb-replace-me
SLACK_CHANNEL=C0AK6UGA1NJ

# Telegram fallback (optional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Runtime
DRY_RUN=0
ENV=dev
STATE_PATH=./data/alert-state.json
LOG_LEVEL=INFO
```

- [ ] **Step 1.6: Create `README.md`**

```markdown
# Based Inventory

Daily bot that monitors Shopify inventory and audits rendered Add-to-Cart state on basedbodyworks.com. Posts alerts to Slack `#alerts-inventory`.

See `docs/specs/2026-04-15-based-inventory-design.md` for the full design.

## Local development

    python3.12 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements-dev.txt
    playwright install chromium
    cp .env.example .env
    # Edit .env with real tokens

    # Run one job locally (dry-run by default via DRY_RUN=1 in .env)
    python -m based_inventory.jobs.quantity_alerts

## Tests

    pytest

## Lint

    ruff check
    ruff format

## Jobs

- `based_inventory.jobs.quantity_alerts` — every 6h
- `based_inventory.jobs.atc_audit` — daily 6am PST (13:00 UTC)
- `based_inventory.jobs.weekly_snapshot` — Fridays 9am PST (16:00 UTC)

Each reads env vars (see `.env.example`) and posts to Slack unless `DRY_RUN=1`.
```

- [ ] **Step 1.7: Create `Dockerfile`**

```dockerfile
FROM mcr.microsoft.com/playwright/python:v1.44.0-focal

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY data/ ./data/

ENV PYTHONPATH=/app/src
ENV PYTHONUNBUFFERED=1

# CMD is overridden per Render service:
#   python -m based_inventory.jobs.quantity_alerts
#   python -m based_inventory.jobs.atc_audit
#   python -m based_inventory.jobs.weekly_snapshot
CMD ["python", "-m", "based_inventory.jobs.quantity_alerts"]
```

- [ ] **Step 1.8: Create `.github/workflows/ci.yml`**

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:

jobs:
  lint-test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -r requirements-dev.txt
      - run: ruff check
      - run: ruff format --check
      - run: pytest
```

- [ ] **Step 1.9: Create `src/based_inventory/__init__.py`**

```python
"""Based Inventory: Shopify inventory monitoring + ATC audit."""

__version__ = "0.1.0"
```

- [ ] **Step 1.10: Create `tests/__init__.py` and `tests/conftest.py`**

`tests/__init__.py`: empty file.

`tests/conftest.py`:

```python
"""Shared pytest fixtures."""

import pytest


@pytest.fixture
def fake_product():
    """Minimal product dict matching the Shopify GraphQL shape."""
    return {
        "id": "gid://shopify/Product/1",
        "title": "Shampoo",
        "handle": "shampoo",
        "totalInventory": 5000,
        "variants": [
            {
                "id": "gid://shopify/ProductVariant/11",
                "title": "Just One",
                "sku": "SHAMPOO-SINGLE",
                "inventoryQuantity": 3000,
                "inventoryPolicy": "DENY",
                "inventoryItem": {
                    "tracked": True,
                    "inventoryLevels": [
                        {"available": 3000, "location": {"id": "loc1", "name": "TX", "shipsInventory": True}},
                    ],
                },
            },
            {
                "id": "gid://shopify/ProductVariant/12",
                "title": "Two Pack",
                "sku": "SHAMPOO-2PACK",
                "inventoryQuantity": 200,
                "inventoryPolicy": "DENY",
                "inventoryItem": {
                    "tracked": True,
                    "inventoryLevels": [
                        {"available": 200, "location": {"id": "loc1", "name": "TX", "shipsInventory": True}},
                    ],
                },
            },
        ],
    }
```

- [ ] **Step 1.11: Set up local venv and verify**

```bash
cd ~/tools/based-inventory
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
playwright install chromium
ruff check
pytest
```

Expected: `ruff` passes with no files to check (no code yet). `pytest` reports "no tests ran."

- [ ] **Step 1.12: Commit scaffolding**

```bash
git add .
git commit -m "chore: project scaffolding (Dockerfile, pyproject, CI)"
```

---

## Phase 2: Config module + Shopify GraphQL client

**Files:**
- Create: `src/based_inventory/config.py`
- Create: `src/based_inventory/shopify.py`
- Create: `tests/test_config.py`
- Create: `tests/test_shopify.py`

- [ ] **Step 2.1: Write failing test for `Config.from_env()`**

`tests/test_config.py`:

```python
"""Tests for env var loading."""

import pytest

from based_inventory.config import Config


def test_config_loads_required_fields(monkeypatch):
    monkeypatch.setenv("SHOPIFY_STORE", "basedbodyworks.myshopify.com")
    monkeypatch.setenv("SHOPIFY_TOKEN", "shpat_test")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "C123")

    cfg = Config.from_env()

    assert cfg.shopify_store == "basedbodyworks.myshopify.com"
    assert cfg.shopify_token == "shpat_test"
    assert cfg.slack_bot_token == "xoxb-test"
    assert cfg.slack_channel == "C123"
    assert cfg.shopify_api_version == "2026-01"
    assert cfg.dry_run is False


def test_config_dry_run_flag(monkeypatch):
    monkeypatch.setenv("SHOPIFY_STORE", "x")
    monkeypatch.setenv("SHOPIFY_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("SLACK_CHANNEL", "x")
    monkeypatch.setenv("DRY_RUN", "1")

    cfg = Config.from_env()

    assert cfg.dry_run is True


def test_config_missing_required_field_raises(monkeypatch):
    monkeypatch.delenv("SHOPIFY_STORE", raising=False)
    monkeypatch.setenv("SHOPIFY_TOKEN", "x")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("SLACK_CHANNEL", "x")

    with pytest.raises(ValueError, match="SHOPIFY_STORE"):
        Config.from_env()
```

- [ ] **Step 2.2: Run test to verify it fails**

```bash
pytest tests/test_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'based_inventory.config'`

- [ ] **Step 2.3: Implement `Config.from_env()`**

`src/based_inventory/config.py`:

```python
"""Runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    shopify_store: str
    shopify_token: str
    shopify_api_version: str
    slack_bot_token: str
    slack_channel: str
    telegram_bot_token: str | None
    telegram_chat_id: str | None
    dry_run: bool
    env: str
    state_path: str
    log_level: str

    @classmethod
    def from_env(cls) -> Config:
        def required(name: str) -> str:
            value = os.getenv(name)
            if not value:
                raise ValueError(f"Missing required env var: {name}")
            return value

        def optional(name: str, default: str | None = None) -> str | None:
            value = os.getenv(name)
            return value if value else default

        return cls(
            shopify_store=required("SHOPIFY_STORE"),
            shopify_token=required("SHOPIFY_TOKEN"),
            shopify_api_version=optional("SHOPIFY_API_VERSION", "2026-01") or "2026-01",
            slack_bot_token=required("SLACK_BOT_TOKEN"),
            slack_channel=required("SLACK_CHANNEL"),
            telegram_bot_token=optional("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=optional("TELEGRAM_CHAT_ID"),
            dry_run=optional("DRY_RUN", "0") == "1",
            env=optional("ENV", "dev") or "dev",
            state_path=optional("STATE_PATH", "./data/alert-state.json") or "./data/alert-state.json",
            log_level=optional("LOG_LEVEL", "INFO") or "INFO",
        )
```

- [ ] **Step 2.4: Run tests to verify they pass**

```bash
pytest tests/test_config.py -v
```

Expected: 3 passed.

- [ ] **Step 2.5: Write failing test for `ShopifyClient.fetch_all_products()`**

`tests/test_shopify.py`:

```python
"""Tests for Shopify GraphQL client."""

from unittest.mock import MagicMock

from based_inventory.shopify import ShopifyClient


def test_fetch_all_products_paginates(monkeypatch):
    responses = [
        {
            "data": {
                "products": {
                    "edges": [
                        {
                            "cursor": "c1",
                            "node": {
                                "id": "gid://shopify/Product/1",
                                "title": "Shampoo",
                                "handle": "shampoo",
                                "totalInventory": 100,
                                "variants": {"edges": [
                                    {"node": {
                                        "id": "gid://shopify/ProductVariant/11",
                                        "title": "Just One",
                                        "sku": "S-1",
                                        "inventoryQuantity": 100,
                                        "inventoryPolicy": "DENY",
                                        "inventoryItem": {
                                            "tracked": True,
                                            "inventoryLevels": {"edges": [
                                                {"node": {"available": 100, "location": {"id": "L1", "name": "TX", "shipsInventory": True}}}
                                            ]},
                                        },
                                    }}
                                ]},
                            },
                        }
                    ],
                    "pageInfo": {"hasNextPage": True},
                }
            }
        },
        {
            "data": {
                "products": {
                    "edges": [
                        {
                            "cursor": "c2",
                            "node": {
                                "id": "gid://shopify/Product/2",
                                "title": "Conditioner",
                                "handle": "conditioner",
                                "totalInventory": 50,
                                "variants": {"edges": []},
                            },
                        }
                    ],
                    "pageInfo": {"hasNextPage": False},
                }
            }
        },
    ]

    mock_post = MagicMock(side_effect=[_mock_response(r) for r in responses])
    monkeypatch.setattr("based_inventory.shopify.requests.post", mock_post)
    monkeypatch.setattr("based_inventory.shopify.time.sleep", lambda _: None)

    client = ShopifyClient(store="test.myshopify.com", token="shpat_test", api_version="2026-01")
    products = client.fetch_all_products()

    assert len(products) == 2
    assert products[0]["title"] == "Shampoo"
    assert products[0]["handle"] == "shampoo"
    assert products[0]["variants"][0]["title"] == "Just One"
    assert products[0]["variants"][0]["inventoryPolicy"] == "DENY"
    assert products[1]["title"] == "Conditioner"
    assert mock_post.call_count == 2


def _mock_response(payload):
    response = MagicMock()
    response.json.return_value = payload
    response.raise_for_status = MagicMock()
    return response
```

- [ ] **Step 2.6: Run test to verify it fails**

```bash
pytest tests/test_shopify.py -v
```

Expected: `ModuleNotFoundError: No module named 'based_inventory.shopify'`

- [ ] **Step 2.7: Implement `ShopifyClient`**

`src/based_inventory/shopify.py`:

```python
"""Shopify Admin GraphQL client."""

from __future__ import annotations

import time
from typing import Any

import requests

_PRODUCTS_QUERY = """
query($cursor: String) {
  products(first: 50, query: "status:active", after: $cursor) {
    edges {
      cursor
      node {
        id
        title
        handle
        totalInventory
        variants(first: 100) {
          edges {
            node {
              id
              title
              sku
              inventoryQuantity
              inventoryPolicy
              inventoryItem {
                tracked
                inventoryLevels(first: 10) {
                  edges {
                    node {
                      available
                      location {
                        id
                        name
                        shipsInventory
                      }
                    }
                  }
                }
              }
            }
          }
        }
      }
    }
    pageInfo { hasNextPage }
  }
}
"""


class ShopifyClient:
    def __init__(self, store: str, token: str, api_version: str = "2026-01") -> None:
        self.endpoint = f"https://{store}/admin/api/{api_version}/graphql.json"
        self.headers = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }

    def _execute(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        response = requests.post(
            self.endpoint,
            json={"query": query, "variables": variables or {}},
            headers=self.headers,
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        if "errors" in payload:
            raise RuntimeError(f"Shopify GraphQL errors: {payload['errors']}")
        return payload

    def fetch_all_products(self) -> list[dict[str, Any]]:
        products: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            payload = self._execute(_PRODUCTS_QUERY, {"cursor": cursor})
            data = payload["data"]["products"]
            for edge in data["edges"]:
                node = edge["node"]
                products.append({
                    "id": node["id"],
                    "title": node["title"],
                    "handle": node["handle"],
                    "totalInventory": node["totalInventory"],
                    "variants": [
                        self._flatten_variant(v["node"]) for v in node["variants"]["edges"]
                    ],
                })
                cursor = edge["cursor"]
            if not data["pageInfo"]["hasNextPage"]:
                break
            time.sleep(1)
        return products

    @staticmethod
    def _flatten_variant(node: dict[str, Any]) -> dict[str, Any]:
        item = node.get("inventoryItem") or {}
        levels_container = item.get("inventoryLevels") or {}
        levels = [
            {
                "available": lvl["node"]["available"],
                "location": lvl["node"]["location"],
            }
            for lvl in levels_container.get("edges", [])
        ]
        return {
            "id": node["id"],
            "title": node["title"],
            "sku": node.get("sku"),
            "inventoryQuantity": node.get("inventoryQuantity", 0),
            "inventoryPolicy": node.get("inventoryPolicy", "DENY"),
            "inventoryItem": {
                "tracked": item.get("tracked", True),
                "inventoryLevels": levels,
            },
        }
```

- [ ] **Step 2.8: Run tests to verify they pass**

```bash
pytest tests/test_shopify.py tests/test_config.py -v
```

Expected: all pass.

- [ ] **Step 2.9: Commit**

```bash
git add src/based_inventory/config.py src/based_inventory/shopify.py tests/test_config.py tests/test_shopify.py
git commit -m "feat: config loader and Shopify GraphQL client"
```

---

## Phase 3: Singles resolver

**Files:**
- Create: `src/based_inventory/singles.py`
- Create: `tests/test_singles.py`

Ported from `check_inventory.py:190-234` and `weekly_audit.py:85-106` with new type hints and unit tests.

- [ ] **Step 3.1: Write failing tests covering all singles patterns**

`tests/test_singles.py`:

```python
"""Tests for singles-variant resolution."""

from based_inventory.singles import resolve_single


def _variant(title, sku=None, qty=0, policy="DENY"):
    return {
        "id": f"gid://shopify/ProductVariant/{abs(hash(title)) % 10_000}",
        "title": title,
        "sku": sku,
        "inventoryQuantity": qty,
        "inventoryPolicy": policy,
        "inventoryItem": {
            "tracked": True,
            "inventoryLevels": [{"available": qty, "location": {"id": "L1", "name": "TX", "shipsInventory": True}}],
        },
    }


def test_single_variant_product_returns_its_qty():
    p = {"title": "Scalp Scrubber", "variants": [_variant("Default Title", qty=5000)]}
    result = resolve_single(p)
    assert result.qty == 5000
    assert result.breakdown is None


def test_just_one_variant_among_packs():
    p = {"title": "Shampoo", "variants": [
        _variant("Just One", qty=3000),
        _variant("Two Pack", qty=200),
        _variant("Three Pack", qty=100),
    ]}
    result = resolve_single(p)
    assert result.qty == 3000


def test_full_size_variant():
    p = {"title": "Toiletry Bag", "variants": [_variant("Full Size", qty=750)]}
    result = resolve_single(p)
    assert result.qty == 750


def test_sku_contains_single():
    p = {"title": "Oddball", "variants": [
        _variant("Standard", sku="PROD-SINGLE", qty=42),
        _variant("Bundle", sku="PROD-BUNDLE", qty=999),
    ]}
    result = resolve_single(p)
    assert result.qty == 42


def test_multi_scent_sums_just_ones():
    p = {"title": "Body Wash", "variants": [
        _variant("Santal Sandalwood / Just One", qty=500),
        _variant("Santal Sandalwood / Two Pack", qty=50),
        _variant("Oud / Just One", qty=300),
        _variant("Oud / Two Pack", qty=40),
    ]}
    result = resolve_single(p)
    assert result.qty == 800
    assert result.breakdown is not None
    assert "Santal Sandalwood: 500" in result.breakdown
    assert "Oud: 300" in result.breakdown


def test_no_single_variant_falls_back_to_total():
    p = {"title": "Weird", "totalInventory": 123, "variants": [
        _variant("Two Pack", qty=999),
        _variant("Three Pack", qty=999),
    ]}
    result = resolve_single(p)
    assert result.qty == 123


def test_case_insensitive_matching():
    p = {"title": "X", "variants": [_variant("JUST ONE", qty=10)]}
    result = resolve_single(p)
    assert result.qty == 10
```

- [ ] **Step 3.2: Run tests to verify they fail**

```bash
pytest tests/test_singles.py -v
```

Expected: all fail with `ModuleNotFoundError`.

- [ ] **Step 3.3: Implement `resolve_single()`**

`src/based_inventory/singles.py`:

```python
"""Singles-variant resolver.

Based products have variants (Just One, Two Pack, Three Pack, multi-scent bundles).
Only the "single" variant controls inventory reality:
- Multi-packs pull from singles at fulfillment time
- A 2-pack shown with qty 50 and singles at 0 is unfulfillable

See INVENTORY-RULES.md in the original Inventory Brain for canonical rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SingleResult:
    qty: int
    breakdown: str | None  # populated for multi-scent sums
    source_variants: list[dict[str, Any]]  # the variants that contributed to the sum


_SINGLE_TITLE_PATTERNS = ("just", "single", "default title", "full size")


def _is_single_variant(variant: dict[str, Any]) -> bool:
    title = (variant.get("title") or "").lower()
    sku = (variant.get("sku") or "").lower()
    if title in ("default title", "full size"):
        return True
    if "just" in title or "single" in title:
        return True
    if "single" in sku:
        return True
    return False


def resolve_single(product: dict[str, Any]) -> SingleResult:
    variants = product.get("variants", [])

    if len(variants) == 1:
        v = variants[0]
        return SingleResult(qty=v.get("inventoryQuantity", 0), breakdown=None, source_variants=[v])

    singles = [v for v in variants if _is_single_variant(v)]

    if not singles:
        return SingleResult(
            qty=product.get("totalInventory", 0),
            breakdown=None,
            source_variants=[],
        )

    if len(singles) == 1:
        v = singles[0]
        return SingleResult(qty=v.get("inventoryQuantity", 0), breakdown=None, source_variants=singles)

    # Multi-scent: sum and build breakdown string
    total = sum(v.get("inventoryQuantity", 0) for v in singles)
    parts = []
    for v in sorted(singles, key=lambda x: x.get("inventoryQuantity", 0)):
        title = v.get("title", "")
        scent = title.split("/")[0].strip() if "/" in title else title
        parts.append(f"{scent}: {v.get('inventoryQuantity', 0):,}")
    breakdown = " | ".join(parts)
    return SingleResult(qty=total, breakdown=breakdown, source_variants=singles)
```

- [ ] **Step 3.4: Run tests to verify they pass**

```bash
pytest tests/test_singles.py -v
```

Expected: 7 passed.

- [ ] **Step 3.5: Commit**

```bash
git add src/based_inventory/singles.py tests/test_singles.py
git commit -m "feat: singles-variant resolver with multi-scent summing"
```

---

## Phase 4: Sets resolver + skip list

**Files:**
- Create: `data/set-components.json` (copied from Inventory Brain)
- Create: `src/based_inventory/sets.py`
- Create: `src/based_inventory/skip_list.py`
- Create: `tests/test_sets.py`
- Create: `tests/test_skip_list.py`

- [ ] **Step 4.1: Copy `set-components.json` from Inventory Brain**

```bash
mkdir -p data
cp ~/Downloads/inventory-brain/set-components.json data/set-components.json
```

Verify contents match the 23 sets in the spec §4.2. No edits.

- [ ] **Step 4.2: Write failing tests for `SetResolver`**

`tests/test_sets.py`:

```python
"""Tests for virtual-bundle resolution."""

import json
from pathlib import Path

import pytest

from based_inventory.sets import SetResolver


@pytest.fixture
def resolver(tmp_path: Path) -> SetResolver:
    components_file = tmp_path / "set-components.json"
    components_file.write_text(json.dumps({
        "sets": {
            "Shower Duo": ["Shampoo", "Conditioner"],
            "Curly Duo": ["Curl Cream", "Leave-In Conditioner"],
            "Daily Skincare Duo": ["Daily Facial Cleanser", "Daily Facial Moisturizer"],
        }
    }))
    return SetResolver(components_path=components_file)


def test_is_set_returns_true_for_known_set(resolver: SetResolver) -> None:
    assert resolver.is_set("Shower Duo") is True
    assert resolver.is_set("Shampoo") is False


def test_components_for_set(resolver: SetResolver) -> None:
    assert resolver.components_for("Shower Duo") == ["Shampoo", "Conditioner"]


def test_sets_containing_component(resolver: SetResolver) -> None:
    assert resolver.sets_containing("Shampoo") == ["Shower Duo"]
    assert resolver.sets_containing("Curl Cream") == ["Curly Duo"]
    assert resolver.sets_containing("Nonexistent") == []


def test_set_capacity_uses_min_component(resolver: SetResolver) -> None:
    singles = {"Shampoo": 4000, "Conditioner": 1500}
    assert resolver.capacity("Shower Duo", singles) == 1500


def test_set_capacity_missing_component_returns_zero(resolver: SetResolver) -> None:
    singles = {"Shampoo": 4000}
    assert resolver.capacity("Shower Duo", singles) == 0


def test_set_capacity_for_unknown_set_raises(resolver: SetResolver) -> None:
    with pytest.raises(KeyError):
        resolver.capacity("Unknown Set", {})
```

- [ ] **Step 4.3: Run tests to verify they fail**

```bash
pytest tests/test_sets.py -v
```

Expected: all fail with `ModuleNotFoundError`.

- [ ] **Step 4.4: Implement `SetResolver`**

`src/based_inventory/sets.py`:

```python
"""Virtual-bundle (set) resolution.

Sets are Shopify products with their own PDPs but no real inventory.
Set capacity = min(component single qty). If any component is at 0,
the set is unfulfillable even if its own inventoryQuantity is positive.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path


class SetResolver:
    def __init__(self, components_path: Path | str) -> None:
        data = json.loads(Path(components_path).read_text())
        self._sets: dict[str, list[str]] = data.get("sets", {})
        self._reverse: dict[str, list[str]] = defaultdict(list)
        for set_name, components in self._sets.items():
            for component in components:
                self._reverse[component].append(set_name)

    def is_set(self, product_title: str) -> bool:
        return product_title in self._sets

    def components_for(self, set_name: str) -> list[str]:
        return list(self._sets[set_name])

    def sets_containing(self, component: str) -> list[str]:
        return list(self._reverse.get(component, []))

    def capacity(self, set_name: str, singles_by_title: dict[str, int]) -> int:
        components = self._sets[set_name]
        return min((singles_by_title.get(c, 0) for c in components), default=0)

    def all_set_names(self) -> list[str]:
        return list(self._sets.keys())
```

- [ ] **Step 4.5: Run tests to verify they pass**

```bash
pytest tests/test_sets.py -v
```

Expected: 6 passed.

- [ ] **Step 4.6: Write failing test for skip list**

`tests/test_skip_list.py`:

```python
"""Tests for the hardcoded product skip list."""

from based_inventory.skip_list import should_skip


def test_shipping_protection_skipped():
    assert should_skip("BASED Shipping Protection") is True
    assert should_skip("Shipping") is True
    assert should_skip("Shipping International") is True


def test_membership_skipped():
    assert should_skip("Based Membership") is True


def test_legacy_products_skipped():
    assert should_skip("Based Shampoo 1.0") is True
    assert should_skip("Based Conditioner 2.0") is True
    assert should_skip("Hair Revival Serum") is True
    assert should_skip("Super Serum") is True
    assert should_skip("Showerhead Filter") is True


def test_samples_skipped():
    assert should_skip("4oz Shampoo + Conditioner Bundle Sample") is True
    assert should_skip("Shampoo + Conditioner Bundle Sample") is True


def test_tshirts_and_accessories_skipped():
    assert should_skip("Bath Stone (White)") is True
    assert should_skip("Based Wooden Comb - Light") is True
    assert should_skip("Brand Ambassador Package") is True


def test_active_products_not_skipped():
    assert should_skip("Shampoo") is False
    assert should_skip("Curl Cream") is False
    assert should_skip("Body Wash") is False
```

- [ ] **Step 4.7: Run test to verify it fails**

```bash
pytest tests/test_skip_list.py -v
```

Expected: all fail.

- [ ] **Step 4.8: Implement skip list**

`src/based_inventory/skip_list.py`:

```python
"""Products excluded from all alerting and auditing.

Merged from Inventory Brain's check_inventory.py SKIP_TITLES and INVENTORY-RULES.md.
Archived products are already filtered by the Shopify query (status:active).
"""

from __future__ import annotations

_SKIP_TITLES: frozenset[str] = frozenset({
    # Shipping and fulfillment
    "Shipping",
    "Shipping International",
    "BASED Shipping Protection",

    # Membership and brand
    "Based Membership",
    "Brand Ambassador Package",

    # Samples
    "Shampoo + Conditioner Bundle Sample",
    "4oz Shampoo + Conditioner Bundle Sample",

    # Accessories (legacy or off-catalog)
    "Bath Stone (White)",
    "Based Wooden Comb - Light",
    "Based Wooden Comb",

    # Legacy formulations (retired product lines)
    "Based Shampoo 1.0",
    "Based Shampoo 2.0",
    "Based Conditioner 1.0",
    "Based Conditioner 2.0",
    "Hair Revival Serum",
    "Super Serum",
    "Showerhead Filter",
})


def should_skip(product_title: str) -> bool:
    return product_title in _SKIP_TITLES
```

- [ ] **Step 4.9: Run tests to verify they pass**

```bash
pytest tests/test_skip_list.py -v
```

Expected: 5 passed.

- [ ] **Step 4.10: Commit**

```bash
git add data/set-components.json src/based_inventory/sets.py src/based_inventory/skip_list.py tests/test_sets.py tests/test_skip_list.py
git commit -m "feat: sets resolver and skip list"
```

---

## Phase 5: Slack Block Kit + posting

**Files:**
- Create: `src/based_inventory/slack.py`
- Create: `tests/test_slack.py`

- [ ] **Step 5.1: Write failing tests for `SlackClient.post_message()`**

`tests/test_slack.py`:

```python
"""Tests for Slack Block Kit poster."""

from unittest.mock import MagicMock

from based_inventory.slack import SlackClient


def test_post_message_success(monkeypatch):
    mock_post = MagicMock(return_value=_mock_response({"ok": True, "ts": "123.456"}))
    monkeypatch.setattr("based_inventory.slack.requests.post", mock_post)

    client = SlackClient(token="xoxb-test", channel="C123")
    ok = client.post_message(fallback_text="hi", blocks=[{"type": "section"}])

    assert ok is True
    args, kwargs = mock_post.call_args
    assert args[0] == "https://slack.com/api/chat.postMessage"
    body = kwargs["json"]
    assert body["channel"] == "C123"
    assert body["text"] == "hi"
    assert body["blocks"] == [{"type": "section"}]
    assert body["unfurl_links"] is False


def test_post_message_returns_false_on_not_ok(monkeypatch):
    mock_post = MagicMock(return_value=_mock_response({"ok": False, "error": "channel_not_found"}))
    monkeypatch.setattr("based_inventory.slack.requests.post", mock_post)

    client = SlackClient(token="xoxb-test", channel="C123")
    ok = client.post_message(fallback_text="hi", blocks=[])

    assert ok is False


def test_dry_run_does_not_call_api(monkeypatch, capsys):
    mock_post = MagicMock()
    monkeypatch.setattr("based_inventory.slack.requests.post", mock_post)

    client = SlackClient(token="xoxb-test", channel="C123", dry_run=True)
    ok = client.post_message(fallback_text="hi", blocks=[{"type": "section"}])

    assert ok is True
    mock_post.assert_not_called()
    captured = capsys.readouterr()
    assert "[DRY_RUN]" in captured.out
    assert "hi" in captured.out


def _mock_response(payload):
    response = MagicMock()
    response.json.return_value = payload
    return response
```

- [ ] **Step 5.2: Run tests to verify they fail**

```bash
pytest tests/test_slack.py -v
```

Expected: fail with `ModuleNotFoundError`.

- [ ] **Step 5.3: Implement `SlackClient`**

`src/based_inventory/slack.py`:

```python
"""Slack Block Kit client for #alerts-inventory posts."""

from __future__ import annotations

import json
import logging
from typing import Any

import requests

logger = logging.getLogger(__name__)

_POST_URL = "https://slack.com/api/chat.postMessage"


class SlackClient:
    def __init__(self, token: str, channel: str, dry_run: bool = False) -> None:
        self.token = token
        self.channel = channel
        self.dry_run = dry_run

    def post_message(self, fallback_text: str, blocks: list[dict[str, Any]]) -> bool:
        payload = {
            "channel": self.channel,
            "text": fallback_text,
            "blocks": blocks,
            "unfurl_links": False,
        }

        if self.dry_run:
            print("[DRY_RUN] Slack post:")
            print(f"  channel: {self.channel}")
            print(f"  text: {fallback_text}")
            print(f"  blocks: {json.dumps(blocks, indent=2)}")
            return True

        try:
            response = requests.post(
                _POST_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            result = response.json()
        except requests.RequestException as exc:
            logger.error("Slack post failed: %s", exc)
            return False

        if not result.get("ok"):
            logger.error("Slack API error: %s", result.get("error", "unknown"))
            return False

        return True


def section(text: str) -> dict[str, Any]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def divider() -> dict[str, Any]:
    return {"type": "divider"}


def header(text: str) -> dict[str, Any]:
    return {"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}


def context(text: str) -> dict[str, Any]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}
```

- [ ] **Step 5.4: Run tests to verify they pass**

```bash
pytest tests/test_slack.py -v
```

Expected: 3 passed.

- [ ] **Step 5.5: Commit**

```bash
git add src/based_inventory/slack.py tests/test_slack.py
git commit -m "feat: Slack Block Kit client with dry-run mode"
```

---

## Phase 6: Telegram fallback (HTTP-based, no external CLI)

**Files:**
- Create: `src/based_inventory/telegram.py`
- Create: `tests/test_telegram.py`

Unlike Inventory Brain (which shells out to `openclaw`), this uses the Telegram Bot API directly over HTTP. Simpler, no external deps.

- [ ] **Step 6.1: Write failing tests for `TelegramFallback.send()`**

`tests/test_telegram.py`:

```python
"""Tests for Telegram HTTP fallback."""

from unittest.mock import MagicMock

from based_inventory.telegram import TelegramFallback


def test_send_calls_bot_api(monkeypatch):
    mock_post = MagicMock(return_value=_mock_response({"ok": True}))
    monkeypatch.setattr("based_inventory.telegram.requests.post", mock_post)

    tg = TelegramFallback(bot_token="12345:abc", chat_id="-100123")
    ok = tg.send("hello")

    assert ok is True
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.telegram.org/bot12345:abc/sendMessage"
    assert kwargs["json"]["chat_id"] == "-100123"
    assert kwargs["json"]["text"] == "hello"


def test_send_no_op_when_not_configured(monkeypatch):
    mock_post = MagicMock()
    monkeypatch.setattr("based_inventory.telegram.requests.post", mock_post)

    tg = TelegramFallback(bot_token=None, chat_id=None)
    ok = tg.send("hello")

    assert ok is True  # not an error, just skipped
    mock_post.assert_not_called()


def test_send_handles_api_error(monkeypatch):
    mock_post = MagicMock(return_value=_mock_response({"ok": False, "description": "chat not found"}))
    monkeypatch.setattr("based_inventory.telegram.requests.post", mock_post)

    tg = TelegramFallback(bot_token="12345:abc", chat_id="bad")
    ok = tg.send("hello")

    assert ok is False


def _mock_response(payload):
    response = MagicMock()
    response.json.return_value = payload
    return response
```

- [ ] **Step 6.2: Run tests to verify they fail**

```bash
pytest tests/test_telegram.py -v
```

Expected: fail with `ModuleNotFoundError`.

- [ ] **Step 6.3: Implement `TelegramFallback`**

`src/based_inventory/telegram.py`:

```python
"""Telegram Bot API fallback for Slack outages.

Unlike Inventory Brain (which uses the `openclaw` CLI), this calls the
Telegram Bot API directly. No external CLI dependency.
"""

from __future__ import annotations

import logging

import requests

logger = logging.getLogger(__name__)


class TelegramFallback:
    def __init__(self, bot_token: str | None, chat_id: str | None) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)

    def send(self, text: str) -> bool:
        if not self.configured:
            logger.info("Telegram fallback not configured; skipping")
            return True

        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        try:
            response = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": text},
                timeout=15,
            )
            result = response.json()
        except requests.RequestException as exc:
            logger.error("Telegram send failed: %s", exc)
            return False

        if not result.get("ok"):
            logger.error("Telegram API error: %s", result.get("description"))
            return False

        return True
```

- [ ] **Step 6.4: Run tests to verify they pass**

```bash
pytest tests/test_telegram.py -v
```

Expected: 3 passed.

- [ ] **Step 6.5: Commit**

```bash
git add src/based_inventory/telegram.py tests/test_telegram.py
git commit -m "feat: Telegram Bot API HTTP fallback"
```

---

## Phase 7: State management (alert dedup)

**Files:**
- Create: `src/based_inventory/state.py`
- Create: `tests/test_state.py`

- [ ] **Step 7.1: Write failing tests for `AlertState`**

`tests/test_state.py`:

```python
"""Tests for persistent alert state."""

import json
from pathlib import Path

from based_inventory.state import AlertState


def test_load_missing_file_returns_empty(tmp_path: Path):
    state = AlertState.load(tmp_path / "missing.json")
    assert state.quantity_tiers == {}
    assert state.atc_flags == {}


def test_load_malformed_file_returns_empty(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("not json")
    state = AlertState.load(path)
    assert state.quantity_tiers == {}


def test_set_and_get_quantity_tier(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.set_tier("Shampoo", 500)
    assert state.get_tier("Shampoo") == 500
    assert state.get_tier("Unknown") is None


def test_crosses_lower_tier_true_on_drop(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.set_tier("Shampoo", 1000)
    assert state.crosses_lower_tier("Shampoo", 500) is True


def test_crosses_lower_tier_false_on_same(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.set_tier("Shampoo", 500)
    assert state.crosses_lower_tier("Shampoo", 500) is False


def test_crosses_lower_tier_false_on_recovery(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.set_tier("Shampoo", 500)
    assert state.crosses_lower_tier("Shampoo", 1000) is False


def test_first_time_ever_crosses_true(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    assert state.crosses_lower_tier("NewProduct", 500) is True


def test_atc_flag_new(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    key = "gid://shopify/ProductVariant/1::/products/x::SALES_LEAK"
    assert state.is_new_atc_flag(key) is True
    state.mark_atc_flag(key, now="2026-04-15T06:00:00Z")
    assert state.is_new_atc_flag(key) is False


def test_save_and_reload(tmp_path: Path):
    path = tmp_path / "s.json"
    state = AlertState.load(path)
    state.set_tier("A", 100)
    state.mark_atc_flag("k1", now="2026-04-15T06:00:00Z")
    state.save(path)

    reloaded = AlertState.load(path)
    assert reloaded.get_tier("A") == 100
    assert reloaded.is_new_atc_flag("k1") is False


def test_clear_atc_flags_not_in_set(tmp_path: Path):
    state = AlertState.load(tmp_path / "s.json")
    state.mark_atc_flag("k1", now="2026-04-15T06:00:00Z")
    state.mark_atc_flag("k2", now="2026-04-15T06:00:00Z")

    state.retain_only_atc_flags({"k1"})

    assert state.is_new_atc_flag("k1") is False
    assert state.is_new_atc_flag("k2") is True
```

- [ ] **Step 7.2: Run tests to verify they fail**

```bash
pytest tests/test_state.py -v
```

Expected: fail with `ModuleNotFoundError`.

- [ ] **Step 7.3: Implement `AlertState`**

`src/based_inventory/state.py`:

```python
"""Persistent alert state for dedup across runs.

Stores the last-observed severity tier per product (quantity alerts)
and the set of currently-flagged ATC anomalies (ATC audit).

File format:
{
  "quantity_tiers": {"Shampoo": 500, "Conditioner": 1000},
  "atc_flags": {
    "<variant_gid>::<url>::<flag_type>": {"first_seen_at": "...", "last_seen_at": "..."}
  }
}
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class AlertState:
    quantity_tiers: dict[str, int] = field(default_factory=dict)
    atc_flags: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | str) -> AlertState:
        p = Path(path)
        if not p.exists():
            return cls()
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not load state from %s: %s; starting fresh", p, exc)
            return cls()
        return cls(
            quantity_tiers=data.get("quantity_tiers", {}),
            atc_flags=data.get("atc_flags", {}),
        )

    def save(self, path: Path | str) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({
            "quantity_tiers": self.quantity_tiers,
            "atc_flags": self.atc_flags,
        }, indent=2))

    # Quantity tier API
    def get_tier(self, product_title: str) -> int | None:
        return self.quantity_tiers.get(product_title)

    def set_tier(self, product_title: str, tier: int) -> None:
        self.quantity_tiers[product_title] = tier

    def clear_tier(self, product_title: str) -> None:
        self.quantity_tiers.pop(product_title, None)

    def crosses_lower_tier(self, product_title: str, new_tier: int) -> bool:
        """True if new_tier represents worse state than previously recorded."""
        prev = self.get_tier(product_title)
        if prev is None:
            return True
        return new_tier < prev

    # ATC flag API
    def is_new_atc_flag(self, key: str) -> bool:
        return key not in self.atc_flags

    def mark_atc_flag(self, key: str, now: str) -> None:
        if key in self.atc_flags:
            self.atc_flags[key]["last_seen_at"] = now
        else:
            self.atc_flags[key] = {"first_seen_at": now, "last_seen_at": now}

    def retain_only_atc_flags(self, keep_keys: set[str]) -> None:
        """Drop ATC flags not in keep_keys (used after a full audit run)."""
        self.atc_flags = {k: v for k, v in self.atc_flags.items() if k in keep_keys}
```

- [ ] **Step 7.4: Run tests to verify they pass**

```bash
pytest tests/test_state.py -v
```

Expected: 10 passed.

- [ ] **Step 7.5: Commit**

```bash
git add src/based_inventory/state.py tests/test_state.py
git commit -m "feat: persistent alert state with tier dedup and ATC flag tracking"
```

---

## Phase 8: `quantity_alerts` job (first working job)

**Files:**
- Create: `src/based_inventory/jobs/__init__.py`
- Create: `src/based_inventory/jobs/_common.py`
- Create: `src/based_inventory/jobs/quantity_alerts.py`
- Create: `tests/test_quantity_alerts.py`

- [ ] **Step 8.1: Create `src/based_inventory/jobs/__init__.py`**

Empty file.

- [ ] **Step 8.2: Create `src/based_inventory/jobs/_common.py`**

Shared error-handling wrapper for all job entrypoints.

```python
"""Common job infrastructure: logging, top-level exception handling, Telegram escalation."""

from __future__ import annotations

import logging
import sys
import traceback
from collections.abc import Callable

from based_inventory.config import Config
from based_inventory.telegram import TelegramFallback

logger = logging.getLogger(__name__)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def run_job(job_name: str, func: Callable[[Config], None]) -> None:
    """Run a job with standard error handling. Logs to stdout; Telegram on fatal error."""
    cfg = Config.from_env()
    configure_logging(cfg.log_level)

    try:
        logger.info("Starting job: %s (env=%s, dry_run=%s)", job_name, cfg.env, cfg.dry_run)
        func(cfg)
        logger.info("Job complete: %s", job_name)
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        logger.error("Job %s FAILED: %s\n%s", job_name, exc, tb)
        tg = TelegramFallback(cfg.telegram_bot_token, cfg.telegram_chat_id)
        tg.send(f"❌ {job_name} FAILED\n\n{type(exc).__name__}: {exc}")
        sys.exit(1)
```

- [ ] **Step 8.3: Write failing test for `build_quantity_alert_blocks()`**

`tests/test_quantity_alerts.py`:

```python
"""Tests for quantity_alerts job block construction."""

from based_inventory.jobs.quantity_alerts import Alert, build_blocks


def test_build_blocks_renders_header_and_sections():
    alerts = [
        Alert(
            label="🚨 CRITICAL",
            product_title="Shampoo",
            qty=50,
            threshold=100,
            variant_info="`SHAMPOO-SINGLE`",
            mention_ids=["U1", "U2", "U3"],
            admin_url="https://admin.shopify.com/store/basedbodyworks/products/1",
            affected_sets=["Shower Duo", "Shower Essentials"],
        ),
    ]

    blocks = build_blocks(alerts)

    assert blocks[0]["type"] == "header"
    assert "Inventory Alert" in blocks[0]["text"]["text"]
    assert blocks[1]["type"] == "divider"

    section = blocks[2]
    assert section["type"] == "section"
    text = section["text"]["text"]
    assert "CRITICAL" in text
    assert "Shampoo" in text
    assert "50" in text
    assert "Shower Duo" in text

    footer = blocks[-1]
    assert footer["type"] == "context"
    assert "<@U1>" in footer["elements"][0]["text"]
    assert "<!channel>" in footer["elements"][0]["text"]
```

- [ ] **Step 8.4: Run test to verify it fails**

```bash
pytest tests/test_quantity_alerts.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 8.5: Implement `quantity_alerts` job**

`src/based_inventory/jobs/quantity_alerts.py`:

```python
"""Every 6h: scan Shopify inventory, post tier-escalation alerts to Slack.

Ported from Inventory Brain's check_inventory.py with:
- Shared modules (config, shopify, singles, sets, state, slack, telegram)
- New type hints and tests
- Telegram via HTTP, not openclaw CLI
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from based_inventory.config import Config
from based_inventory.jobs._common import run_job
from based_inventory.sets import SetResolver
from based_inventory.shopify import ShopifyClient
from based_inventory.singles import resolve_single
from based_inventory.skip_list import should_skip
from based_inventory.slack import SlackClient, context, divider, header, section
from based_inventory.state import AlertState

# Slack user IDs (confirmed against Inventory Brain check_inventory.py:35-37)
CARLOS = "U08LCCS7V5Z"
RYAN = "U0AHP969HC5"
ALEX = "U09GXTYGT44"

# Threshold ladder (ported from check_inventory.py:39-44)
THRESHOLDS: list[tuple[int, str, list[str]]] = [
    (100, "🚨 CRITICAL", [CARLOS, RYAN, ALEX]),
    (500, "🔴 LOW STOCK", [CARLOS]),
    (750, "🟠 WARNING", [CARLOS]),
    (1000, "🟡 HEADS UP", []),
]

STORE_ADMIN = "https://admin.shopify.com/store/basedbodyworks"
COMPONENTS_PATH = Path(__file__).resolve().parents[3] / "data" / "set-components.json"


@dataclass
class Alert:
    label: str
    product_title: str
    qty: int
    threshold: int
    variant_info: str
    mention_ids: list[str]
    admin_url: str
    affected_sets: list[str]


def _tier_for(qty: int) -> tuple[int, str, list[str]] | None:
    for threshold, label, mentions in THRESHOLDS:
        if qty <= threshold:
            return threshold, label, mentions
    return None


def _product_id(gid: str) -> str:
    return gid.rsplit("/", 1)[-1]


def build_blocks(alerts: list[Alert]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        header("⚡ Inventory Alert"),
        divider(),
    ]

    for a in alerts:
        text = (
            f"{a.label}  —  <{a.admin_url}|*{a.product_title}*>\n"
            f"📦  *{a.qty:,}* singles remaining"
        )
        if a.variant_info:
            text += f"\n{a.variant_info}"
        if a.affected_sets:
            text += f"\n⚠️  _Bottleneck for: {', '.join(a.affected_sets)}_"
        blocks.append(section(text))

    blocks.append(divider())

    ts = time.strftime("%b %d, %I:%M %p PST", time.gmtime(time.time() - 7 * 3600))
    footer_text = f"🕐  {ts}"

    all_mentions: list[str] = []
    for a in alerts:
        for uid in a.mention_ids:
            if uid not in all_mentions:
                all_mentions.append(uid)
    if all_mentions:
        footer_text += "\n\n" + "  ".join(f"<@{uid}>" for uid in all_mentions)
    footer_text += "\n<!channel>"

    blocks.append(context(footer_text))
    return blocks


def _run(cfg: Config) -> None:
    shopify = ShopifyClient(cfg.shopify_store, cfg.shopify_token, cfg.shopify_api_version)
    set_resolver = SetResolver(COMPONENTS_PATH)
    state = AlertState.load(cfg.state_path)
    slack = SlackClient(cfg.slack_bot_token, cfg.slack_channel, dry_run=cfg.dry_run)

    products = shopify.fetch_all_products()
    alerts: list[Alert] = []
    new_tiers: dict[str, int] = {}

    for product in products:
        title = product["title"]

        if should_skip(title):
            continue
        if set_resolver.is_set(title):
            continue

        result = resolve_single(product)
        if result.qty < 0:
            continue

        tier = _tier_for(result.qty)
        if tier is None:
            state.clear_tier(title)
            continue

        threshold, label, mentions = tier
        new_tiers[title] = threshold

        if state.crosses_lower_tier(title, threshold):
            variant_info = f"`{result.source_variants[0]['sku']}`" if (
                result.source_variants and result.source_variants[0].get("sku")
            ) else ""
            if result.breakdown:
                variant_info = result.breakdown
            alerts.append(Alert(
                label=label,
                product_title=title,
                qty=result.qty,
                threshold=threshold,
                variant_info=variant_info,
                mention_ids=mentions,
                admin_url=f"{STORE_ADMIN}/products/{_product_id(product['id'])}",
                affected_sets=set_resolver.sets_containing(title),
            ))

    # Update state: overwrite quantity_tiers with the current scan's active tiers
    state.quantity_tiers = new_tiers
    state.save(cfg.state_path)

    if not alerts:
        return

    alerts.sort(key=lambda a: a.qty)
    blocks = build_blocks(alerts)
    fallback = f"⚡ Inventory Alert — {len(alerts)} product(s) below threshold"
    slack.post_message(fallback, blocks)


def main() -> None:
    run_job("quantity_alerts", _run)


if __name__ == "__main__":
    main()
```

- [ ] **Step 8.6: Run tests to verify they pass**

```bash
pytest tests/test_quantity_alerts.py -v
```

Expected: passes.

- [ ] **Step 8.7: Local smoke test (dry-run) against dev store**

Requires `.env` populated with dev Shopify token.

```bash
source .venv/bin/activate
DRY_RUN=1 python -m based_inventory.jobs.quantity_alerts 2>&1 | head -80
```

Expected output: either "no alerts below threshold" or a `[DRY_RUN] Slack post:` block listing any real alerts from the dev store.

- [ ] **Step 8.8: Commit**

```bash
git add src/based_inventory/jobs/
git add tests/test_quantity_alerts.py
git commit -m "feat: quantity_alerts job (every 6h Shopify scan + Slack alert)"
```

---

### CHECKPOINT 1: `quantity_alerts` works end-to-end

Before moving on, verify:
- [ ] Full test suite passes (`pytest`)
- [ ] Lint passes (`ruff check`)
- [ ] Dry-run against dev store produces sensible output
- [ ] Optional: flip `DRY_RUN=0` and post one real test alert to a private Slack channel (not `#alerts-inventory`) to confirm the Slack token and Block Kit rendering. Revert channel before continuing.

Do not deploy to Render yet. Proceed to Phase 9.

---

## Phase 9: `weekly_snapshot` job (Inventory Brain parity)

**Files:**
- Create: `src/based_inventory/jobs/weekly_snapshot.py`
- Create: `tests/test_weekly_snapshot.py`

Ported from `weekly_audit.py` (23 hardcoded products across 6 categories).

- [ ] **Step 9.1: Write failing test for `build_snapshot_blocks()`**

`tests/test_weekly_snapshot.py`:

```python
"""Tests for weekly_snapshot block construction."""

from based_inventory.jobs.weekly_snapshot import ProductLine, build_snapshot_blocks


def test_snapshot_renders_categories():
    sections = [
        ("Hair Care", [
            ProductLine(name="Shampoo", qty=3000, breakdown=None, pack2=None, affected_sets=[]),
            ProductLine(name="Conditioner", qty=500, breakdown=None, pack2=250, affected_sets=["Shower Duo"]),
        ]),
        ("Body", [
            ProductLine(name="Body Wash", qty=800, breakdown="Santal: 500 · Oud: 300", pack2=None, affected_sets=["Body Care Set"]),
        ]),
    ]

    blocks = build_snapshot_blocks(sections, date_str="Apr 15, 2026")

    assert blocks[0]["type"] == "header"
    assert "Weekly Inventory Audit" in blocks[0]["text"]["text"]

    texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert any("*Hair Care*" in t for t in texts)
    assert any("*Body*" in t for t in texts)
    combined = "\n".join(texts)
    assert "3,000" in combined
    assert "500" in combined
    assert "Shower Duo" in combined
    assert "Santal" in combined
    assert "2-pack: 250" in combined

    legend = blocks[-1]["elements"][0]["text"]
    assert "5K+" in legend
    assert "Oversold" in legend
```

- [ ] **Step 9.2: Run test to verify it fails**

```bash
pytest tests/test_weekly_snapshot.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 9.3: Implement `weekly_snapshot` job**

`src/based_inventory/jobs/weekly_snapshot.py`:

```python
"""Fridays 9am PST: post full inventory snapshot to Slack.

Ported from Inventory Brain's weekly_audit.py. Tracks 23 products across
6 categories at single-variant level. Sets excluded (constrained by lowest component).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from based_inventory.config import Config
from based_inventory.jobs._common import run_job
from based_inventory.sets import SetResolver
from based_inventory.shopify import ShopifyClient
from based_inventory.singles import resolve_single
from based_inventory.slack import SlackClient, context, divider, header, section

LOW = 1000

# Same layout as weekly_audit.py:22-29. 'special' indicates variant-of-Daily-Skincare-Duo.
AUDIT_LAYOUT: list[tuple[str, list[tuple[str, str | None]]]] = [
    ("Hair Care", [("Shampoo", None), ("Conditioner", None), ("Hair Elixir", None)]),
    ("Straight/Wavy Styling", [("Texture Powder", None), ("Sea Salt Spray", None), ("Pomade", None), ("Hair Clay", None)]),
    ("Curly Styling", [("Leave-In Conditioner", None), ("Curl Cream", None), ("Curl Mousse", None), ("Curl Gel", None), ("Curl Refresh Spray", None)]),
    ("Body", [("Body Wash", None), ("Body Lotion", None), ("Deodorant", None)]),
    ("Skin", [("Facial Cleanser", "special"), ("Facial Moisturizer", "special"), ("Skin Revival Spray", None), ("Under Eye Elixir", None), ("Tallow Moisturizer", None)]),
    ("Accessories", [("Toiletry Bag", None), ("Scalp Scrubber", None), ("Wooden Hair Comb", None)]),
]

COMPONENTS_PATH = Path(__file__).resolve().parents[3] / "data" / "set-components.json"


@dataclass
class ProductLine:
    name: str
    qty: int
    breakdown: str | None
    pack2: int | None
    affected_sets: list[str]


def _emoji(qty: int) -> str:
    if qty < 0:
        return "⛔"
    if qty <= 100:
        return "🚨"
    if qty <= 500:
        return "🔴"
    if qty <= 750:
        return "🟠"
    if qty <= 1000:
        return "🟡"
    if qty <= 5000:
        return "📊"
    return "🟢"


def _twopack_qty(product: dict[str, Any]) -> int | None:
    twos = [
        v for v in product["variants"]
        if any(p in (v.get("title") or "").lower() for p in ("two pack", "pack of 2", "2pck"))
        and "just" not in (v.get("title") or "").lower()
        and "single" not in (v.get("title") or "").lower()
    ]
    if not twos:
        return None
    return sum(v.get("inventoryQuantity", 0) for v in twos)


def _render_line(line: ProductLine) -> str:
    text = f"{_emoji(line.qty)} {line.name} — *{line.qty:,}*"
    if line.breakdown:
        text += f"  ({line.breakdown})"
    if 0 < line.qty <= LOW and line.pack2 is not None:
        text += f"  ·  2-pack: {line.pack2:,}"
    if line.qty <= LOW and line.affected_sets:
        text += f" → {', '.join(line.affected_sets)}"
    return text


def build_snapshot_blocks(sections: list[tuple[str, list[ProductLine]]], date_str: str) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        header("📦 Weekly Inventory Audit"),
        section(
            "Tracking *23* products at *single-variant level* (not inflated totals)\n"
            "Sets excluded — constrained by lowest component  |  🗓️ " + date_str
        ),
        divider(),
    ]

    for category, lines in sections:
        body = "*" + category + "*\n" + "\n".join(_render_line(line) for line in lines)
        blocks.append(section(body))

    blocks.append(divider())
    blocks.append(context(
        "🟢 5K+  ·  📊 1K-5K  ·  🟡 ≤1K  ·  🟠 ≤750  ·  🔴 ≤500  ·  🚨 ≤100  ·  ⛔ Oversold\n<!channel>"
    ))
    return blocks


def _run(cfg: Config) -> None:
    shopify = ShopifyClient(cfg.shopify_store, cfg.shopify_token, cfg.shopify_api_version)
    set_resolver = SetResolver(COMPONENTS_PATH)
    slack = SlackClient(cfg.slack_bot_token, cfg.slack_channel, dry_run=cfg.dry_run)

    products = shopify.fetch_all_products()
    by_title = {p["title"]: p for p in products}

    # Daily Facial Cleanser / Moisturizer live as variants of Daily Skincare Duo
    dsd = by_title.get("Daily Skincare Duo")
    facial_cleanser_qty: int | None = None
    facial_moisturizer_qty: int | None = None
    if dsd:
        for v in dsd.get("variants", []):
            lower = (v.get("title") or "").lower()
            if "cleanser" in lower:
                facial_cleanser_qty = v.get("inventoryQuantity")
            elif "moisturizer" in lower:
                facial_moisturizer_qty = v.get("inventoryQuantity")

    sections: list[tuple[str, list[ProductLine]]] = []
    for category, entries in AUDIT_LAYOUT:
        lines: list[ProductLine] = []
        for name, special in entries:
            if special == "special":
                qty = facial_cleanser_qty if name == "Facial Cleanser" else facial_moisturizer_qty
                rev_key = "Daily Facial Cleanser" if name == "Facial Cleanser" else "Daily Facial Moisturizer"
                if qty is None:
                    lines.append(ProductLine(name=f"❓ {name} — not found", qty=0, breakdown=None, pack2=None, affected_sets=[]))
                    continue
                lines.append(ProductLine(
                    name=name,
                    qty=qty,
                    breakdown=None,
                    pack2=None,
                    affected_sets=set_resolver.sets_containing(rev_key),
                ))
                continue

            product = by_title.get(name)
            if not product:
                lines.append(ProductLine(name=f"❓ {name} — not found", qty=0, breakdown=None, pack2=None, affected_sets=[]))
                continue

            result = resolve_single(product)
            lines.append(ProductLine(
                name=name,
                qty=result.qty,
                breakdown=result.breakdown,
                pack2=_twopack_qty(product) if 0 < result.qty <= LOW else None,
                affected_sets=set_resolver.sets_containing(name),
            ))

        sections.append((category, lines))

    date_str = time.strftime("%b %d, %Y")
    blocks = build_snapshot_blocks(sections, date_str)
    fallback = f"📦 Weekly Inventory Audit — {date_str}"
    slack.post_message(fallback, blocks)


def main() -> None:
    run_job("weekly_snapshot", _run)


if __name__ == "__main__":
    main()
```

- [ ] **Step 9.4: Run tests to verify they pass**

```bash
pytest tests/test_weekly_snapshot.py -v
```

Expected: passes.

- [ ] **Step 9.5: Local smoke test (dry-run)**

```bash
DRY_RUN=1 python -m based_inventory.jobs.weekly_snapshot 2>&1 | head -120
```

Expected: `[DRY_RUN] Slack post:` with a full snapshot of all 23 products by category. Any "❓ not found" rows indicate dev-store products that don't match production names (acceptable for dev testing).

- [ ] **Step 9.6: Commit**

```bash
git add src/based_inventory/jobs/weekly_snapshot.py tests/test_weekly_snapshot.py
git commit -m "feat: weekly_snapshot job (Fridays 9am PST Block Kit full audit)"
```

---

### CHECKPOINT 2: Inventory Brain parity achieved

After Phase 9, the new bot matches Inventory Brain's functional coverage (quantity alerts + weekly snapshot). At this point the parallel-run experiment could start if you wanted early validation:

- [ ] Optional: deploy to Render in DRY_RUN mode (see Phase 15) and compare output with Inventory Brain's live posts for one week.

Otherwise, keep building. Proceed to Phase 10 for the ATC audit.

---

## Phase 10: URL enumeration

**Files:**
- Create: `src/based_inventory/crawl/__init__.py`
- Create: `src/based_inventory/crawl/urls.py`
- Create: `tests/test_urls.py`
- Create: `tests/fixtures/sitemap_index.xml`
- Create: `tests/fixtures/sitemap_pages.xml`
- Create: `tests/fixtures/products_page_1.json`

- [ ] **Step 10.1: Create `src/based_inventory/crawl/__init__.py`**

Empty file.

- [ ] **Step 10.2: Create fixture files**

`tests/fixtures/sitemap_index.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://basedbodyworks.com/sitemap_products_1.xml</loc></sitemap>
  <sitemap><loc>https://basedbodyworks.com/sitemap_pages_1.xml</loc></sitemap>
  <sitemap><loc>https://basedbodyworks.com/sitemap_collections_1.xml</loc></sitemap>
</sitemapindex>
```

`tests/fixtures/sitemap_pages.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://basedbodyworks.com/pages/spring-launch</loc></url>
  <url><loc>https://basedbodyworks.com/pages/curly-routine</loc></url>
  <url><loc>https://basedbodyworks.com/pages/about</loc></url>
</urlset>
```

`tests/fixtures/products_page_1.json`:

```json
{
  "products": [
    {"id": 1, "handle": "shampoo", "title": "Shampoo"},
    {"id": 2, "handle": "curl-cream", "title": "Curl Cream"}
  ]
}
```

- [ ] **Step 10.3: Write failing tests for URL enumeration**

`tests/test_urls.py`:

```python
"""Tests for URL enumeration (sitemap + products.json)."""

from pathlib import Path
from unittest.mock import MagicMock

from based_inventory.crawl.urls import UrlEnumerator

FIXTURES = Path(__file__).parent / "fixtures"


def _response(text="", json_data=None):
    r = MagicMock()
    r.text = text
    r.raise_for_status = MagicMock()
    if json_data is not None:
        r.json.return_value = json_data
    return r


def test_pdp_urls_from_products_json(monkeypatch):
    products_json = (FIXTURES / "products_page_1.json").read_text()
    import json as _json
    responses = [_response(json_data=_json.loads(products_json)), _response(json_data={"products": []})]
    mock_get = MagicMock(side_effect=responses)
    monkeypatch.setattr("based_inventory.crawl.urls.requests.get", mock_get)

    enumerator = UrlEnumerator(store_url="https://basedbodyworks.com")
    urls = enumerator.pdp_urls()

    assert "https://basedbodyworks.com/products/shampoo" in urls
    assert "https://basedbodyworks.com/products/curl-cream" in urls
    assert len(urls) == 2


def test_landing_pages_from_sitemap(monkeypatch):
    sitemap_index = (FIXTURES / "sitemap_index.xml").read_text()
    sitemap_pages = (FIXTURES / "sitemap_pages.xml").read_text()

    def get(url, **_kw):
        if url.endswith("sitemap.xml"):
            return _response(text=sitemap_index)
        if "sitemap_pages" in url:
            return _response(text=sitemap_pages)
        return _response(text='<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')

    monkeypatch.setattr("based_inventory.crawl.urls.requests.get", MagicMock(side_effect=get))

    enumerator = UrlEnumerator(store_url="https://basedbodyworks.com")
    urls = enumerator.landing_page_urls()

    assert "https://basedbodyworks.com/pages/spring-launch" in urls
    assert "https://basedbodyworks.com/pages/curly-routine" in urls
    assert "https://basedbodyworks.com/pages/about" in urls
```

- [ ] **Step 10.4: Run tests to verify they fail**

```bash
pytest tests/test_urls.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 10.5: Implement `UrlEnumerator`**

`src/based_inventory/crawl/urls.py`:

```python
"""Discover every URL a product can appear on.

Three sources:
1. `/products.json?page=N` — PDP URLs
2. `/sitemap.xml` → follow index → filter `/pages/*` — Instant Commerce landing pages
3. `/collections.json` — collection PLP URLs (also in sitemap but parsing JSON is safer)
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)

_SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"


@dataclass(frozen=True)
class EnumeratedUrls:
    pdp: list[str]
    collection: list[str]
    landing: list[str]


class UrlEnumerator:
    def __init__(self, store_url: str) -> None:
        self.store_url = store_url.rstrip("/")

    def pdp_urls(self) -> list[str]:
        urls: list[str] = []
        page = 1
        while True:
            resp = requests.get(
                f"{self.store_url}/products.json",
                params={"limit": 250, "page": page},
                timeout=30,
            )
            resp.raise_for_status()
            products = resp.json().get("products", [])
            if not products:
                break
            urls.extend(f"{self.store_url}/products/{p['handle']}" for p in products)
            page += 1
        return urls

    def collection_urls(self) -> list[str]:
        urls: list[str] = []
        page = 1
        while True:
            resp = requests.get(
                f"{self.store_url}/collections.json",
                params={"limit": 250, "page": page},
                timeout=30,
            )
            resp.raise_for_status()
            collections = resp.json().get("collections", [])
            if not collections:
                break
            urls.extend(f"{self.store_url}/collections/{c['handle']}" for c in collections)
            page += 1
        return urls

    def landing_page_urls(self) -> list[str]:
        index_resp = requests.get(f"{self.store_url}/sitemap.xml", timeout=30)
        index_resp.raise_for_status()
        index_root = ET.fromstring(index_resp.text)

        sitemap_urls = [
            elem.text.strip()
            for elem in index_root.findall(f"{_SITEMAP_NS}sitemap/{_SITEMAP_NS}loc")
            if elem.text and "sitemap_pages" in elem.text
        ]

        urls: list[str] = []
        for sitemap_url in sitemap_urls:
            try:
                resp = requests.get(sitemap_url, timeout=30)
                resp.raise_for_status()
                root = ET.fromstring(resp.text)
                for loc in root.findall(f"{_SITEMAP_NS}url/{_SITEMAP_NS}loc"):
                    if loc.text and "/pages/" in loc.text:
                        urls.append(loc.text.strip())
            except (requests.RequestException, ET.ParseError) as exc:
                logger.warning("Could not parse sitemap %s: %s", sitemap_url, exc)
        return urls

    def enumerate_all(self) -> EnumeratedUrls:
        return EnumeratedUrls(
            pdp=self.pdp_urls(),
            collection=self.collection_urls(),
            landing=self.landing_page_urls(),
        )
```

- [ ] **Step 10.6: Run tests to verify they pass**

```bash
pytest tests/test_urls.py -v
```

Expected: 2 passed.

- [ ] **Step 10.7: Commit**

```bash
git add src/based_inventory/crawl/
git add tests/test_urls.py tests/fixtures/
git commit -m "feat: URL enumeration via sitemap + products.json + collections.json"
```

---

## Phase 11: ATC selectors and HTML parser (no browser yet)

**Files:**
- Create: `src/based_inventory/crawl/selectors.py`
- Create: `src/based_inventory/crawl/atc_parser.py`
- Create: `tests/test_atc_parser.py`
- Create: `tests/fixtures/pdp_in_stock.html`
- Create: `tests/fixtures/pdp_sold_out.html`
- Create: `tests/fixtures/no_atc_element.html`

This phase covers the pure-logic part of ATC detection so we can unit-test it without Playwright. Phase 12 wires this into real browser-rendered pages.

- [ ] **Step 11.1: Create HTML fixtures**

`tests/fixtures/pdp_in_stock.html`:

```html
<!DOCTYPE html>
<html><body>
<form action="/cart/add" id="product-form-123">
  <button type="submit" name="add" class="product-form__submit">Add to cart</button>
</form>
</body></html>
```

`tests/fixtures/pdp_sold_out.html`:

```html
<!DOCTYPE html>
<html><body>
<form action="/cart/add" id="product-form-123">
  <button type="submit" name="add" disabled aria-disabled="true" class="product-form__submit sold-out">Sold out</button>
</form>
</body></html>
```

`tests/fixtures/no_atc_element.html`:

```html
<!DOCTYPE html>
<html><body>
<div class="product-card">
  <h2>Some Product</h2>
  <a href="/products/some-product">View details</a>
</div>
</body></html>
```

- [ ] **Step 11.2: Write failing tests for `parse_atc_state()`**

`tests/test_atc_parser.py`:

```python
"""Tests for ATC state parsing from rendered HTML."""

from pathlib import Path

from based_inventory.crawl.atc_parser import AtcState, parse_atc_state

FIXTURES = Path(__file__).parent / "fixtures"


def test_in_stock_pdp_parsed_as_sellable():
    html = (FIXTURES / "pdp_in_stock.html").read_text()
    state = parse_atc_state(html)
    assert state == AtcState(present=True, enabled=True, text="Add to cart")


def test_sold_out_pdp_parsed_as_oos():
    html = (FIXTURES / "pdp_sold_out.html").read_text()
    state = parse_atc_state(html)
    assert state.present is True
    assert state.enabled is False
    assert "sold out" in state.text.lower()


def test_page_without_atc_element_returns_missing():
    html = (FIXTURES / "no_atc_element.html").read_text()
    state = parse_atc_state(html)
    assert state.present is False
    assert state.enabled is False
```

- [ ] **Step 11.3: Run tests to verify they fail**

```bash
pytest tests/test_atc_parser.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 11.4: Implement selectors and parser**

`src/based_inventory/crawl/selectors.py`:

```python
"""ATC button selector list with fallbacks.

Ordered from most-specific to most-fuzzy. First match wins.
Runs against rendered DOM (Playwright) in the crawler, and against
static HTML (BeautifulSoup) in unit-test fallback.
"""

from __future__ import annotations

# CSS selectors tried in order
ATC_SELECTORS: list[str] = [
    'button[name="add"]',
    'button[type="submit"][form*="product-form"]',
    'form[action*="/cart/add"] button[type="submit"]',
    '[data-testid*="add-to-cart" i]',
    '[data-atc]',  # common Instant Commerce attribute; update after live inspection
]

# Text regex for fallback matching on button innerText
ATC_TEXT_PATTERN = r"(?i)(add to cart|sold out|notify me|coming soon)"
```

Add `beautifulsoup4` to `requirements.txt`:

```bash
cd ~/tools/based-inventory
echo "beautifulsoup4==4.12.3" >> requirements.txt
pip install beautifulsoup4==4.12.3
```

`src/based_inventory/crawl/atc_parser.py`:

```python
"""Parse ATC state from rendered HTML (static analysis).

Used in tests with saved fixtures, and as a fallback in the live crawler
when Playwright's dynamic queries fail. For the full crawler (variant-aware,
dynamic rendering), see `atc.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from bs4 import BeautifulSoup, Tag

from based_inventory.crawl.selectors import ATC_SELECTORS, ATC_TEXT_PATTERN


@dataclass(frozen=True)
class AtcState:
    present: bool
    enabled: bool
    text: str

    @classmethod
    def missing(cls) -> AtcState:
        return cls(present=False, enabled=False, text="")


_SOLD_OUT_PATTERNS = ("sold-out", "sold_out", "unavailable", "soldout")


def _is_disabled(tag: Tag) -> bool:
    if tag.has_attr("disabled"):
        return True
    aria = (tag.get("aria-disabled") or "").lower()
    if aria == "true":
        return True
    class_attr = " ".join(tag.get("class") or []).lower()
    return any(p in class_attr for p in _SOLD_OUT_PATTERNS)


def parse_atc_state(html: str) -> AtcState:
    soup = BeautifulSoup(html, "html.parser")

    for selector in ATC_SELECTORS:
        element = soup.select_one(selector)
        if element:
            return AtcState(
                present=True,
                enabled=not _is_disabled(element),
                text=element.get_text(strip=True) or "",
            )

    # Text-based fallback
    pattern = re.compile(ATC_TEXT_PATTERN)
    candidate = soup.find(["button", "a"], string=pattern)
    if candidate:
        return AtcState(
            present=True,
            enabled=not _is_disabled(candidate),
            text=candidate.get_text(strip=True) or "",
        )

    return AtcState.missing()
```

- [ ] **Step 11.5: Run tests to verify they pass**

```bash
pytest tests/test_atc_parser.py -v
```

Expected: 3 passed.

- [ ] **Step 11.6: Commit**

```bash
git add src/based_inventory/crawl/selectors.py src/based_inventory/crawl/atc_parser.py
git add tests/test_atc_parser.py tests/fixtures/pdp_*.html tests/fixtures/no_atc_element.html
git add requirements.txt
git commit -m "feat: ATC selector strategy and static HTML parser"
```

---

## Phase 12: Playwright crawl orchestration

**Files:**
- Create: `src/based_inventory/crawl/atc.py`
- Create: `tests/test_atc_crawler.py`

Live browser rendering. Tests use Playwright's `Page.set_content()` to stub a live page, so no real network calls in tests.

- [ ] **Step 12.1: Write failing test for `AtcCrawler.audit_url()`**

`tests/test_atc_crawler.py`:

```python
"""Tests for Playwright-driven ATC audit.

Uses Page.set_content() to avoid real network. Requires playwright install
during test setup (already in requirements-dev.txt).
"""

import pytest

from based_inventory.crawl.atc import AtcCrawler, VariantObservation

pytestmark = pytest.mark.playwright


def test_static_page_with_in_stock_atc(tmp_path):
    """Single-variant product page with a present+enabled ATC button."""
    html = """
    <html><body>
      <form id="product-form-1" action="/cart/add">
        <button name="add" type="submit">Add to cart</button>
      </form>
    </body></html>
    """
    with AtcCrawler(headless=True) as crawler:
        observations = crawler.audit_inline_html(html, url="https://test.invalid/products/x")
    assert len(observations) == 1
    obs = observations[0]
    assert obs.present is True
    assert obs.enabled is True
    assert "add to cart" in obs.text.lower()


def test_sold_out_page():
    html = """
    <html><body>
      <form id="product-form-1" action="/cart/add">
        <button name="add" type="submit" disabled aria-disabled="true">Sold out</button>
      </form>
    </body></html>
    """
    with AtcCrawler(headless=True) as crawler:
        observations = crawler.audit_inline_html(html, url="https://test.invalid/products/y")
    obs = observations[0]
    assert obs.present is True
    assert obs.enabled is False
    assert "sold out" in obs.text.lower()


def test_no_atc_element_page():
    html = "<html><body><h1>Nothing here</h1></body></html>"
    with AtcCrawler(headless=True) as crawler:
        observations = crawler.audit_inline_html(html, url="https://test.invalid/pages/a")
    obs = observations[0]
    assert obs.present is False
```

Add `playwright` marker to `pyproject.toml` `[tool.pytest.ini_options]`:

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
python_files = ["test_*.py"]
addopts = "-v --tb=short"
markers = ["playwright: tests that require a running browser"]
```

- [ ] **Step 12.2: Run tests to verify they fail**

```bash
pytest tests/test_atc_crawler.py -v
```

Expected: `ModuleNotFoundError: No module named 'based_inventory.crawl.atc'`.

- [ ] **Step 12.3: Implement `AtcCrawler`**

`src/based_inventory/crawl/atc.py`:

```python
"""Playwright-driven ATC state detection.

Responsibilities:
1. Launch one chromium browser, reuse across URLs
2. For each URL: open page, wait for network idle, read ATC state
3. For PDP: iterate variant picker options, re-read ATC state per variant
4. Return structured VariantObservation list

Bot-detection mitigations: stealth User-Agent, throttled concurrency,
random jitter between page loads.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from types import TracebackType
from typing import Self

from playwright.sync_api import Browser, Page, Playwright, sync_playwright

from based_inventory.crawl.selectors import ATC_SELECTORS

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class VariantObservation:
    url: str
    variant_label: str | None  # None if single-variant page or collection card
    present: bool
    enabled: bool
    text: str


class AtcCrawler:
    def __init__(self, headless: bool = True, throttle_ms: tuple[int, int] = (500, 1500)) -> None:
        self.headless = headless
        self.throttle_ms = throttle_ms
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    def __enter__(self) -> Self:
        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self.headless)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._browser:
            self._browser.close()
        if self._playwright:
            self._playwright.stop()

    def _new_page(self) -> Page:
        if self._browser is None:
            raise RuntimeError("AtcCrawler must be used as a context manager")
        ctx = self._browser.new_context(user_agent=_USER_AGENT)
        return ctx.new_page()

    def _throttle(self) -> None:
        low, high = self.throttle_ms
        time.sleep(random.uniform(low, high) / 1000)

    def _read_state_on_current_page(self, page: Page) -> tuple[bool, bool, str]:
        for selector in ATC_SELECTORS:
            locator = page.locator(selector).first
            if locator.count() > 0:
                try:
                    disabled = locator.is_disabled()
                    aria = (locator.get_attribute("aria-disabled") or "").lower()
                    klass = (locator.get_attribute("class") or "").lower()
                    text = (locator.inner_text(timeout=2000) or "").strip()
                    enabled = not (disabled or aria == "true" or "sold-out" in klass or "soldout" in klass)
                    return True, enabled, text
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Selector %s hit but attribute read failed: %s", selector, exc)
                    continue
        return False, False, ""

    def audit_url(self, url: str) -> list[VariantObservation]:
        """Audit a single URL. For a PDP with variants, iterate variants."""
        page = self._new_page()
        try:
            page.goto(url, wait_until="networkidle", timeout=30_000)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to load %s: %s", url, exc)
            return [VariantObservation(url=url, variant_label=None, present=False, enabled=False, text="")]

        observations: list[VariantObservation] = []

        # Default: read state with no variant selected
        present, enabled, text = self._read_state_on_current_page(page)
        observations.append(VariantObservation(
            url=url, variant_label=None, present=present, enabled=enabled, text=text,
        ))

        # TODO (Phase 12.4): iterate variant picker options.
        # Held back to keep this step small. Implemented in 12.4.

        self._throttle()
        page.context.close()
        return observations

    def audit_inline_html(self, html: str, url: str) -> list[VariantObservation]:
        """Test helper: set page content directly instead of navigating."""
        page = self._new_page()
        page.set_content(html, wait_until="load")
        present, enabled, text = self._read_state_on_current_page(page)
        page.context.close()
        return [VariantObservation(url=url, variant_label=None, present=present, enabled=enabled, text=text)]
```

- [ ] **Step 12.4: Run tests to verify they pass**

```bash
pytest tests/test_atc_crawler.py -v
```

Expected: 3 passed.

- [ ] **Step 12.5: Extend `audit_url()` to iterate variant pickers**

Replace the `# TODO (Phase 12.4)` block in `src/based_inventory/crawl/atc.py` inside `audit_url()` with:

```python
        # Detect and iterate variant picker (Shopify pattern: radio inputs or select)
        variant_inputs = page.locator('fieldset input[type="radio"][name*="option" i]').all()
        if variant_inputs:
            for radio in variant_inputs:
                try:
                    label = radio.get_attribute("value") or radio.get_attribute("aria-label") or ""
                    radio.click()
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Variant click failed: %s", exc)
                    continue
                present, enabled, text = self._read_state_on_current_page(page)
                observations.append(VariantObservation(
                    url=url, variant_label=label.strip() or None,
                    present=present, enabled=enabled, text=text,
                ))

        else:
            # Fallback: look for native <select> variant picker
            select_options = page.locator('select[name*="id" i] option[value]').all()
            for option in select_options:
                try:
                    label = option.inner_text(timeout=2000).strip() or (option.get_attribute("value") or "")
                    value = option.get_attribute("value")
                    if not value:
                        continue
                    page.locator('select[name*="id" i]').first.select_option(value=value)
                    page.wait_for_load_state("networkidle", timeout=5000)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("Variant select failed: %s", exc)
                    continue
                present, enabled, text = self._read_state_on_current_page(page)
                observations.append(VariantObservation(
                    url=url, variant_label=label or None,
                    present=present, enabled=enabled, text=text,
                ))
```

- [ ] **Step 12.6: Run full test suite**

```bash
pytest -v
```

Expected: all tests pass.

- [ ] **Step 12.7: Commit**

```bash
git add src/based_inventory/crawl/atc.py tests/test_atc_crawler.py pyproject.toml
git commit -m "feat: Playwright ATC crawler with variant iteration"
```

---

## Phase 13: ATC diff and flag generation

**Files:**
- Create: `src/based_inventory/crawl/diff.py`
- Create: `tests/test_diff.py`

- [ ] **Step 13.1: Write failing tests for `generate_flags()`**

`tests/test_diff.py`:

```python
"""Tests for expected-vs-observed ATC state diffing."""

from based_inventory.crawl.atc import VariantObservation
from based_inventory.crawl.diff import ExpectedState, Flag, FlagType, generate_flags


def _obs(url, variant=None, present=True, enabled=True, text="Add to cart"):
    return VariantObservation(url=url, variant_label=variant, present=present, enabled=enabled, text=text)


def test_match_produces_no_flag():
    expected = ExpectedState(sellable=True, inventory_policy="DENY")
    obs = _obs("https://x/products/a")
    flags = generate_flags(expected, obs, variant_gid="v1", product_title="Shampoo")
    assert flags == []


def test_sales_leak_when_sellable_but_atc_disabled():
    expected = ExpectedState(sellable=True, inventory_policy="DENY")
    obs = _obs("https://x/products/a", enabled=False, text="Sold out")
    flags = generate_flags(expected, obs, variant_gid="v1", product_title="Shampoo")
    assert len(flags) == 1
    assert flags[0].flag_type == FlagType.SALES_LEAK


def test_oversell_risk_when_oos_but_atc_enabled():
    expected = ExpectedState(sellable=False, inventory_policy="DENY")
    obs = _obs("https://x/products/a", enabled=True, text="Add to cart")
    flags = generate_flags(expected, obs, variant_gid="v1", product_title="Shampoo")
    assert len(flags) == 1
    assert flags[0].flag_type == FlagType.OVERSELL_RISK


def test_no_buy_button_when_missing():
    expected = ExpectedState(sellable=True, inventory_policy="DENY")
    obs = _obs("https://x/pages/x", present=False, enabled=False, text="")
    flags = generate_flags(expected, obs, variant_gid="v1", product_title="Shampoo")
    assert len(flags) == 1
    assert flags[0].flag_type == FlagType.NO_BUY_BUTTON


def test_inventory_policy_continue_suppresses_oversell_risk():
    """inventoryPolicy=CONTINUE means backorder allowed; ATC enabled when OOS is intentional."""
    expected = ExpectedState(sellable=False, inventory_policy="CONTINUE")
    obs = _obs("https://x/products/a", enabled=True, text="Add to cart")
    flags = generate_flags(expected, obs, variant_gid="v1", product_title="Shampoo")
    assert flags == []


def test_flag_state_key_uniqueness():
    expected = ExpectedState(sellable=True, inventory_policy="DENY")
    obs = _obs("https://x/products/a", enabled=False, text="Sold out")
    flag = generate_flags(expected, obs, variant_gid="gid://shopify/ProductVariant/11", product_title="S")[0]
    assert flag.state_key == "gid://shopify/ProductVariant/11::https://x/products/a::SALES_LEAK"
```

- [ ] **Step 13.2: Run tests to verify they fail**

```bash
pytest tests/test_diff.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 13.3: Implement `generate_flags()`**

`src/based_inventory/crawl/diff.py`:

```python
"""Expected-vs-observed ATC state diff → flag generation."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from based_inventory.crawl.atc import VariantObservation


class FlagType(str, Enum):
    SALES_LEAK = "SALES_LEAK"
    OVERSELL_RISK = "OVERSELL_RISK"
    NO_BUY_BUTTON = "NO_BUY_BUTTON"


@dataclass(frozen=True)
class ExpectedState:
    sellable: bool
    inventory_policy: str  # "DENY" or "CONTINUE"


@dataclass(frozen=True)
class Flag:
    flag_type: FlagType
    product_title: str
    variant_gid: str
    variant_label: str | None
    url: str
    expected_sellable: bool
    observed_text: str
    state_key: str


def _make_key(variant_gid: str, url: str, flag_type: FlagType) -> str:
    return f"{variant_gid}::{url}::{flag_type.value}"


def generate_flags(
    expected: ExpectedState,
    observed: VariantObservation,
    variant_gid: str,
    product_title: str,
) -> list[Flag]:
    """Return list of flags for this (expected, observed) pair. Empty list if state matches."""
    # NO_BUY_BUTTON always wins when ATC element is missing
    if not observed.present:
        return [Flag(
            flag_type=FlagType.NO_BUY_BUTTON,
            product_title=product_title,
            variant_gid=variant_gid,
            variant_label=observed.variant_label,
            url=observed.url,
            expected_sellable=expected.sellable,
            observed_text=observed.text,
            state_key=_make_key(variant_gid, observed.url, FlagType.NO_BUY_BUTTON),
        )]

    observed_sellable = observed.enabled and not _is_sold_out_text(observed.text)

    if expected.sellable and not observed_sellable:
        return [Flag(
            flag_type=FlagType.SALES_LEAK,
            product_title=product_title,
            variant_gid=variant_gid,
            variant_label=observed.variant_label,
            url=observed.url,
            expected_sellable=expected.sellable,
            observed_text=observed.text,
            state_key=_make_key(variant_gid, observed.url, FlagType.SALES_LEAK),
        )]

    if not expected.sellable and observed_sellable:
        if expected.inventory_policy.upper() == "CONTINUE":
            # Backorder allowed; ATC enabled when OOS is intentional
            return []
        return [Flag(
            flag_type=FlagType.OVERSELL_RISK,
            product_title=product_title,
            variant_gid=variant_gid,
            variant_label=observed.variant_label,
            url=observed.url,
            expected_sellable=expected.sellable,
            observed_text=observed.text,
            state_key=_make_key(variant_gid, observed.url, FlagType.OVERSELL_RISK),
        )]

    return []


def _is_sold_out_text(text: str) -> bool:
    lowered = text.lower()
    return any(p in lowered for p in ("sold out", "notify me", "coming soon", "unavailable"))
```

- [ ] **Step 13.4: Run tests to verify they pass**

```bash
pytest tests/test_diff.py -v
```

Expected: 6 passed.

- [ ] **Step 13.5: Commit**

```bash
git add src/based_inventory/crawl/diff.py tests/test_diff.py
git commit -m "feat: ATC expected-vs-observed diff with inventoryPolicy suppression"
```

---

## Phase 14: `atc_audit` job (new behavior)

**Files:**
- Create: `src/based_inventory/jobs/atc_audit.py`
- Create: `tests/test_atc_audit.py`

- [ ] **Step 14.1: Write failing test for `build_atc_blocks()`**

`tests/test_atc_audit.py`:

```python
"""Tests for atc_audit Slack block construction."""

from based_inventory.crawl.diff import Flag, FlagType
from based_inventory.jobs.atc_audit import build_atc_blocks


def test_atc_blocks_includes_all_flag_types_and_v0_footer():
    flags = [
        Flag(
            flag_type=FlagType.SALES_LEAK,
            product_title="Curl Cream",
            variant_gid="gid1",
            variant_label="Two Pack",
            url="https://basedbodyworks.com/products/curl-cream",
            expected_sellable=True,
            observed_text="Sold out",
            state_key="gid1::...::SALES_LEAK",
        ),
        Flag(
            flag_type=FlagType.OVERSELL_RISK,
            product_title="Shower Duo",
            variant_gid="gid2",
            variant_label=None,
            url="https://basedbodyworks.com/products/shower-duo",
            expected_sellable=False,
            observed_text="Add to cart",
            state_key="gid2::...::OVERSELL_RISK",
        ),
        Flag(
            flag_type=FlagType.NO_BUY_BUTTON,
            product_title="Leave-In Conditioner",
            variant_gid="gid3",
            variant_label=None,
            url="https://basedbodyworks.com/pages/spring-launch",
            expected_sellable=True,
            observed_text="",
            state_key="gid3::...::NO_BUY_BUTTON",
        ),
    ]

    blocks = build_atc_blocks(flags)

    texts = "\n".join(b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b)
    assert "SALES LEAK" in texts
    assert "Curl Cream" in texts
    assert "OVERSELL RISK" in texts
    assert "Shower Duo" in texts
    assert "NO BUY BUTTON" in texts
    assert "Leave-In Conditioner" in texts

    # v0 limitation footer on OVERSELL RISK rows
    assert "v0 limitation" in texts
    assert "ShipHero" in texts

    footer = blocks[-1]["elements"][0]["text"]
    assert "<!channel>" in footer
    assert "<@U08LCCS7V5Z>" in footer  # Carlos for OVERSELL
    assert "<@U0AHP969HC5>" in footer  # Ryan for SALES LEAK / NO BUY
```

- [ ] **Step 14.2: Run test to verify it fails**

```bash
pytest tests/test_atc_audit.py -v
```

Expected: `ModuleNotFoundError`.

- [ ] **Step 14.3: Implement `atc_audit` job**

`src/based_inventory/jobs/atc_audit.py`:

```python
"""Daily 6am PST: crawl site, diff ATC vs Shopify truth, alert on disagreements.

Flow:
1. Pull Shopify products with inventory + inventoryPolicy per variant
2. Enumerate URLs (PDPs + collections + /pages/* via sitemap)
3. Compute expected sellable per variant using singles-only math
4. Playwright-render each URL, observe ATC state per variant
5. Diff → flags (SALES_LEAK / OVERSELL_RISK / NO_BUY_BUTTON)
6. Dedup against alert-state.json (post only NEW flags)
7. Post Block Kit alert, prune resolved flags from state
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from based_inventory.config import Config
from based_inventory.crawl.atc import AtcCrawler, VariantObservation
from based_inventory.crawl.diff import ExpectedState, Flag, FlagType, generate_flags
from based_inventory.crawl.urls import UrlEnumerator
from based_inventory.jobs._common import run_job
from based_inventory.sets import SetResolver
from based_inventory.shopify import ShopifyClient
from based_inventory.singles import resolve_single
from based_inventory.skip_list import should_skip
from based_inventory.slack import SlackClient, context, divider, header, section
from based_inventory.state import AlertState

logger = logging.getLogger(__name__)

# User IDs (same as quantity_alerts)
CARLOS = "U08LCCS7V5Z"
RYAN = "U0AHP969HC5"
ALEX = "U09GXTYGT44"

FLAG_ICONS = {
    FlagType.SALES_LEAK: "🛒",
    FlagType.OVERSELL_RISK: "⚠️",
    FlagType.NO_BUY_BUTTON: "👻",
}

FLAG_LABELS = {
    FlagType.SALES_LEAK: "SALES LEAK",
    FlagType.OVERSELL_RISK: "OVERSELL RISK",
    FlagType.NO_BUY_BUTTON: "NO BUY BUTTON",
}

FLAG_MENTIONS = {
    FlagType.OVERSELL_RISK: [CARLOS],
    FlagType.SALES_LEAK: [ALEX, RYAN],
    FlagType.NO_BUY_BUTTON: [ALEX, RYAN],
}

V0_LIMITATION_FOOTER = (
    "⚠️ v0 limitation: trusts Shopify as source of truth. "
    "Shopify and ShipHero can drift. Verify against ShipHero before action."
)

COMPONENTS_PATH = Path(__file__).resolve().parents[3] / "data" / "set-components.json"


@dataclass(frozen=True)
class ExpectedVariant:
    """Expected state for one (product, variant) under singles-only math."""
    variant_gid: str
    product_title: str
    variant_label: str
    expected: ExpectedState


def _sellable_from_levels(variant: dict[str, Any]) -> bool:
    levels = variant.get("inventoryItem", {}).get("inventoryLevels", [])
    return any(
        lvl["available"] > 0 and lvl["location"].get("shipsInventory", True)
        for lvl in levels
    )


def _single_variant_qty_for_product(product: dict[str, Any]) -> int:
    """Use resolve_single to get the effective single qty."""
    return resolve_single(product).qty


def compute_expected_states(
    products: list[dict[str, Any]],
    set_resolver: SetResolver,
) -> dict[str, ExpectedVariant]:
    """Return gid → ExpectedVariant for every variant we'll check."""
    expected_by_gid: dict[str, ExpectedVariant] = {}
    singles_by_title = {p["title"]: _single_variant_qty_for_product(p) for p in products}

    for product in products:
        title = product["title"]
        if should_skip(title):
            continue
        # Skip the Daily Facial Cleanser/Moisturizer special case: no PDP to crawl
        if title in ("Daily Facial Cleanser", "Daily Facial Moisturizer"):
            continue

        is_set = set_resolver.is_set(title)

        for variant in product["variants"]:
            gid = variant["id"]
            policy = variant.get("inventoryPolicy", "DENY")
            variant_title = variant.get("title") or ""
            lower = variant_title.lower()

            if is_set:
                # Set variants resolve to min component single
                capacity = set_resolver.capacity(title, singles_by_title)
                sellable = capacity > 0
            elif "two pack" in lower or "2 pack" in lower:
                single_qty = singles_by_title.get(title, 0)
                sellable = (single_qty // 2) >= 1
            elif "three pack" in lower or "3 pack" in lower:
                single_qty = singles_by_title.get(title, 0)
                sellable = (single_qty // 3) >= 1
            elif any(x in lower for x in ("just", "single")) or lower in ("default title", "full size"):
                sellable = _sellable_from_levels(variant)
            else:
                # Unknown variant shape: fall back to its own availability
                sellable = _sellable_from_levels(variant)

            expected_by_gid[gid] = ExpectedVariant(
                variant_gid=gid,
                product_title=title,
                variant_label=variant_title,
                expected=ExpectedState(sellable=sellable, inventory_policy=policy),
            )

    return expected_by_gid


def build_atc_blocks(flags: list[Flag]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = [
        header(f"⚡ ATC Audit — {len(flags)} disagreement(s)"),
        divider(),
    ]

    # Sort: OVERSELL_RISK first (most urgent), then SALES_LEAK, then NO_BUY_BUTTON
    order = {FlagType.OVERSELL_RISK: 0, FlagType.SALES_LEAK: 1, FlagType.NO_BUY_BUTTON: 2}
    sorted_flags = sorted(flags, key=lambda f: (order[f.flag_type], f.product_title))

    for f in sorted_flags:
        icon = FLAG_ICONS[f.flag_type]
        label = FLAG_LABELS[f.flag_type]
        variant_part = f" / {f.variant_label}" if f.variant_label else ""
        body = (
            f"{icon} {label} — *{f.product_title}*{variant_part}\n"
            f"🔗 <{f.url}|{f.url}>\n"
            f"💬 Observed: \"{f.observed_text}\""
        )
        if f.flag_type == FlagType.OVERSELL_RISK:
            body += f"\n{V0_LIMITATION_FOOTER}"
        blocks.append(section(body))

    blocks.append(divider())

    ts = time.strftime("%b %d, %I:%M %p PST", time.gmtime(time.time() - 7 * 3600))
    all_mentions: list[str] = []
    for f in flags:
        for uid in FLAG_MENTIONS[f.flag_type]:
            if uid not in all_mentions:
                all_mentions.append(uid)
    footer_text = f"🕐 {ts}"
    if all_mentions:
        footer_text += "\n\n" + "  ".join(f"<@{uid}>" for uid in all_mentions)
    footer_text += "\n<!channel>"
    blocks.append(context(footer_text))
    return blocks


def _extract_pdp_handle(url: str) -> str | None:
    """Return product handle if URL matches `/products/{handle}` pattern."""
    marker = "/products/"
    if marker not in url:
        return None
    tail = url.split(marker, 1)[1]
    handle = tail.split("/")[0].split("?")[0]
    return handle or None


def _match_variant(obs: VariantObservation, candidates: list[ExpectedVariant]) -> ExpectedVariant | None:
    """Match an observation to an expected variant by variant_label (case-insensitive)."""
    if not candidates:
        return None
    if obs.variant_label is None:
        # Default view (no variant selected): pick the single variant if unique,
        # or the "Just One" / "Default Title" / "Full Size" variant by convention.
        defaults = [
            ev for ev in candidates
            if any(x in ev.variant_label.lower() for x in ("just", "single", "default title", "full size"))
        ]
        if defaults:
            return defaults[0]
        return candidates[0] if len(candidates) == 1 else None

    label = obs.variant_label.lower().strip()
    for ev in candidates:
        if ev.variant_label.lower().strip() == label:
            return ev
    # Partial match fallback (e.g., "Santal Sandalwood" obs vs "Santal Sandalwood / Just One" expected)
    for ev in candidates:
        if label in ev.variant_label.lower() or ev.variant_label.lower() in label:
            return ev
    return None


def _run(cfg: Config) -> None:
    shopify = ShopifyClient(cfg.shopify_store, cfg.shopify_token, cfg.shopify_api_version)
    set_resolver = SetResolver(COMPONENTS_PATH)
    state = AlertState.load(cfg.state_path)
    slack = SlackClient(cfg.slack_bot_token, cfg.slack_channel, dry_run=cfg.dry_run)
    url_enum = UrlEnumerator(f"https://{cfg.shopify_store.replace('.myshopify.com', '.com')}")
    # Note: UrlEnumerator uses the public storefront domain. For Based, that's basedbodyworks.com.
    # Adjust if dev store uses a different public domain.

    logger.info("Fetching Shopify products")
    products = shopify.fetch_all_products()
    expected = compute_expected_states(products, set_resolver)

    # Index variants by (product_handle, variant_title_lower) for PDP-variant matching
    handle_to_variants: dict[str, list[ExpectedVariant]] = {}
    for product in products:
        handle = product.get("handle")
        if not handle:
            continue
        handle_to_variants[handle] = [
            ev for ev in expected.values() if ev.product_title == product["title"]
        ]

    logger.info("Enumerating URLs")
    urls = url_enum.enumerate_all()
    all_urls = list(dict.fromkeys(urls.pdp + urls.collection + urls.landing))
    logger.info("Auditing %d URLs", len(all_urls))

    all_flags: list[Flag] = []
    with AtcCrawler(headless=True) as crawler:
        for url in all_urls:
            try:
                observations = crawler.audit_url(url)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Crawl failed for %s: %s", url, exc)
                continue

            handle = _extract_pdp_handle(url)
            if handle and handle in handle_to_variants:
                # PDP: match observations to expected variants by variant_label
                pdp_variants = handle_to_variants[handle]
                for obs in observations:
                    ev = _match_variant(obs, pdp_variants)
                    if ev is None:
                        continue
                    all_flags.extend(generate_flags(
                        expected=ev.expected,
                        observed=obs,
                        variant_gid=ev.variant_gid,
                        product_title=ev.product_title,
                    ))
            else:
                # Non-PDP URL (collection or landing page): v0 only flags NO_BUY_BUTTON
                # when the entire page has zero ATC elements. Full product-card matching
                # is a v0.5 enhancement.
                if all(not obs.present for obs in observations):
                    # Page rendered but no ATC found at all. Synthesize a page-level flag.
                    all_flags.append(Flag(
                        flag_type=FlagType.NO_BUY_BUTTON,
                        product_title="(page-level, no ATC detected)",
                        variant_gid=f"page::{url}",
                        variant_label=None,
                        url=url,
                        expected_sellable=True,
                        observed_text="",
                        state_key=f"page::{url}::NO_BUY_BUTTON",
                    ))

    logger.info("Found %d flags", len(all_flags))

    # Dedup: only post NEW flags
    now_iso = datetime.now(UTC).isoformat()
    new_flags = [f for f in all_flags if state.is_new_atc_flag(f.state_key)]

    # Update state with ALL current flags (including persistent ones), prune resolved
    state.retain_only_atc_flags({f.state_key for f in all_flags})
    for f in all_flags:
        state.mark_atc_flag(f.state_key, now=now_iso)
    state.save(cfg.state_path)

    if not new_flags:
        logger.info("No new flags to post")
        return

    blocks = build_atc_blocks(new_flags)
    fallback = f"⚡ ATC Audit — {len(new_flags)} new disagreement(s)"
    slack.post_message(fallback, blocks)


def main() -> None:
    run_job("atc_audit", _run)


if __name__ == "__main__":
    main()
```

- [ ] **Step 14.4: Run tests to verify they pass**

```bash
pytest tests/test_atc_audit.py -v
```

Expected: passes.

- [ ] **Step 14.5: Local smoke test against dev store**

```bash
DRY_RUN=1 python -m based_inventory.jobs.atc_audit 2>&1 | tail -80
```

Expected: logs show URL enumeration counts, crawl progress, and either "no new flags" or a `[DRY_RUN] Slack post:` block. First run may take 5-15 minutes depending on catalog size. If Cloudflare challenges appear repeatedly, see §13.2 of the spec.

- [ ] **Step 14.6: Commit**

```bash
git add src/based_inventory/jobs/atc_audit.py tests/test_atc_audit.py
git commit -m "feat: atc_audit job (daily site crawl + ATC/inventory diff)"
```

---

### CHECKPOINT 3: full system working on dev

Before deploying to Render, verify:
- [ ] `pytest` passes all tests
- [ ] `ruff check` passes
- [ ] `ruff format --check` passes
- [ ] All three jobs run locally in dry-run without exceptions against dev store
- [ ] Spot-check one PDP the crawler flagged: load it in browser and confirm the flag is real
- [ ] Spot-check the dev store's inventory tiers against `quantity_alerts` output
- [ ] ATC crawl completes in under 20 minutes on the full catalog

If crawl time is over 20 minutes, increase Playwright concurrency in `atc.py` or split the job into multiple Render services (PDPs vs collections vs landing pages).

---

## Phase 15: Render deployment

**Files:**
- Create: `render.yaml` (infrastructure-as-code, optional but nice)

Manual work in Render dashboard (Phase 0 prerequisite: Render account exists).

- [ ] **Step 15.1: Create `render.yaml`**

```yaml
services:
  - type: cron
    name: based-inventory-quantity
    runtime: docker
    dockerfilePath: ./Dockerfile
    schedule: "0 */6 * * *"
    dockerCommand: python -m based_inventory.jobs.quantity_alerts
    plan: starter
    disk:
      name: state
      mountPath: /data
      sizeGB: 1
    envVars:
      - key: SHOPIFY_STORE
        sync: false
      - key: SHOPIFY_TOKEN
        sync: false
      - key: SLACK_BOT_TOKEN
        sync: false
      - key: SLACK_CHANNEL
        value: C0AK6UGA1NJ
      - key: TELEGRAM_BOT_TOKEN
        sync: false
      - key: TELEGRAM_CHAT_ID
        sync: false
      - key: STATE_PATH
        value: /data/alert-state.json
      - key: ENV
        value: prod
      - key: DRY_RUN
        value: "1"

  - type: cron
    name: based-inventory-atc
    runtime: docker
    dockerfilePath: ./Dockerfile
    schedule: "0 13 * * *"
    dockerCommand: python -m based_inventory.jobs.atc_audit
    plan: starter
    disk:
      name: state
      mountPath: /data
      sizeGB: 1
    envVars:
      - fromService:
          name: based-inventory-quantity
          type: cron
          envVarKey: SHOPIFY_STORE
        key: SHOPIFY_STORE
      - fromService:
          name: based-inventory-quantity
          type: cron
          envVarKey: SHOPIFY_TOKEN
        key: SHOPIFY_TOKEN
      - fromService:
          name: based-inventory-quantity
          type: cron
          envVarKey: SLACK_BOT_TOKEN
        key: SLACK_BOT_TOKEN
      - key: SLACK_CHANNEL
        value: C0AK6UGA1NJ
      - key: STATE_PATH
        value: /data/alert-state.json
      - key: ENV
        value: prod
      - key: DRY_RUN
        value: "1"

  - type: cron
    name: based-inventory-weekly
    runtime: docker
    dockerfilePath: ./Dockerfile
    schedule: "0 16 * * 5"
    dockerCommand: python -m based_inventory.jobs.weekly_snapshot
    plan: starter
    envVars:
      - fromService:
          name: based-inventory-quantity
          type: cron
          envVarKey: SHOPIFY_STORE
        key: SHOPIFY_STORE
      - fromService:
          name: based-inventory-quantity
          type: cron
          envVarKey: SHOPIFY_TOKEN
        key: SHOPIFY_TOKEN
      - fromService:
          name: based-inventory-quantity
          type: cron
          envVarKey: SLACK_BOT_TOKEN
        key: SLACK_BOT_TOKEN
      - key: SLACK_CHANNEL
        value: C0AK6UGA1NJ
      - key: ENV
        value: prod
      - key: DRY_RUN
        value: "1"
```

Notes:
- `DRY_RUN=1` is set for all three services at deploy time. Flip to `0` at cutover.
- `SLACK_CHANNEL` defaults to `#alerts-inventory` (`C0AK6UGA1NJ`); override during parallel-run to a private test channel.
- `fromService` references cross-wire shared secrets so you only set them once on the quantity service.

- [ ] **Step 15.2: Commit and push to main**

```bash
git add render.yaml
git commit -m "feat: Render infrastructure-as-code for three cron services"
git push origin main
```

- [ ] **Step 15.3: Connect Render to the GitHub repo**

Manual in Render dashboard:
1. New → Blueprint
2. Connect `basedbodyworks-apps/based-inventory` repo
3. Render auto-detects `render.yaml` and provisions all three cron services
4. Fill in the `sync: false` env vars (SHOPIFY_TOKEN, SLACK_BOT_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) in the Render UI for the `based-inventory-quantity` service. The other two services inherit via `fromService`.

- [ ] **Step 15.4: Verify first build succeeds**

Check Render build logs for each service. Expect Playwright base image to pull, requirements to install, and first cron to be scheduled.

- [ ] **Step 15.5: Trigger one manual run per service, confirm DRY_RUN output in Render logs**

Render UI → each service → "Trigger Run". Expect `[DRY_RUN] Slack post:` output in logs. No real posts yet.

---

## Phase 16: Cutover to production

- [ ] **Step 16.1: Parallel-run week with `DRY_RUN=1`**

All three Render services post dry-run output to logs only. Copy the logs daily to a scratch Slack DM with Avi for comparison with Inventory Brain's live posts. Reconcile any material differences in flag counts or tier crossings.

Expected differences (all acceptable):
- New bot's "sellable" definition is per-location-that-ships, whereas Inventory Brain sums total inventory. Small correctness improvement.
- New ATC flags don't have an Inventory Brain counterpart. Validate against manual site inspection.

If anything else differs, root-cause before flipping live.

- [ ] **Step 16.2: Pre-flight checklist before flipping live**

- [ ] Spot-check one quantity alert, one ATC flag, and one weekly snapshot line against Shopify Admin
- [ ] Confirm bot display name reads "Based Inventory" in Slack workspace admin
- [ ] Confirm `#alerts-inventory` is the right channel (no dev-only target lingering)
- [ ] Confirm Telegram fallback fires when Slack token is deliberately revoked (test in a separate service, not prod)
- [ ] Confirm `alert-state.json` is persisting across runs on Render Disk

- [ ] **Step 16.3: Reset state file and flip DRY_RUN to 0**

```bash
# In Render Shell on the quantity service:
echo '{}' > /data/alert-state.json
```

Then in Render UI for each of the three services: set `DRY_RUN=0`. Save and redeploy.

- [ ] **Step 16.4: Verify first live posts**

- The next scheduled `quantity_alerts` run posts to `#alerts-inventory` as "Based Inventory"
- Weekly snapshot posts that Friday at 9am PST
- ATC audit posts the next day at 6am PST

- [ ] **Step 16.5: Coordinate Inventory Brain retirement**

Message the colleague running Inventory Brain (per the project memory, it runs on their laptop):
- "Based Inventory is live in `#alerts-inventory`. Please stop `check_inventory.py` and `weekly_audit.py` on your laptop at your convenience. Let me know when done and I'll post a 'Based Inventory has taken over' note in the channel."

- [ ] **Step 16.6: Post takeover note in #alerts-inventory**

After Inventory Brain is stopped, post a one-line context message as the new bot:
> "Based Inventory is now the source of truth for this channel (replacing Inventory Brain). New alert types: 🛒 SALES LEAK, ⚠️ OVERSELL RISK, 👻 NO BUY BUTTON. v0 treats Shopify as truth; ShipHero reconciliation coming in v1. Spec in [GitHub link]."

---

## Self-review notes (completed inline during writing)

- Spec coverage: every in-scope section (§1–§14) maps to at least one task. §15 "Open items" is handled as prerequisites or post-launch followups.
- No placeholders: every step contains runnable commands or real code.
- Type consistency: `AlertState`, `ShopifyClient`, `SetResolver`, `SlackClient`, `TelegramFallback`, `UrlEnumerator`, `AtcCrawler`, `Flag`, `FlagType`, `ExpectedState`, `VariantObservation`, `ExpectedVariant`, `SingleResult`, `ProductLine`, `Alert` are defined once and referenced consistently.
- Scope check: plan covers one system (not independent subsystems). Phased for natural checkpoint boundaries but deploys as one codebase.

---

## Post-launch backlog (not in this plan)

Tracked here for reference; opens as GitHub issues after cutover:

- ShipHero API integration for three-way drift detection (new `INVENTORY DRIFT` alert class)
- Next.js dashboard reading from Postgres / Supabase with flag history and screenshots
- Screenshot artifacts on R2 linked from Slack alerts
- Linear ticket auto-creation on new SALES LEAK / NO BUY BUTTON
- Optional `publishedScope` auto-hide for persistent OVERSELL RISK (>24h) with policy approval
- Instant Commerce admin API integration (replace sitemap-based landing page enumeration)
- Selector regression nightly test (alert if detected ATC count drops > 20% day-over-day)
- DST-aware timestamps (replace `UTC - 7h` with `zoneinfo`)
