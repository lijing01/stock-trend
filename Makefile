PYTHON ?= python3
SKILL_DIR := .claude/skills/stock-trend
SCRIPTS_DIR := $(SKILL_DIR)/scripts
TESTS_DIR := $(SKILL_DIR)/tests

.PHONY: syntax test-unit test-integration test-golden diff-check diff-check-staged check

syntax:
	$(PYTHON) -m compileall -q $(SCRIPTS_DIR)

test-unit:
	$(PYTHON) -m unittest discover -s $(TESTS_DIR) -p 'test_*.py'

test-integration:
	$(PYTHON) $(TESTS_DIR)/test_stock_trend.py

test-golden:
	$(PYTHON) $(TESTS_DIR)/test_golden.py --diff

diff-check:
	git diff --check

diff-check-staged:
	git diff --cached --check

check: syntax test-unit test-integration test-golden diff-check
