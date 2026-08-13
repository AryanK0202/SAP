from __future__ import annotations

import argparse
import unittest

from sap_validate import _build_limit, _filter_tags_for_components, _selected_hosts


class RunnerSelectionTests(unittest.TestCase):
    def test_hana_component_keeps_host_and_hana_tags(self):
        catalog = {
            "checks": [
                {"ansible_tag": "login", "scope": "host", "component": "operating_system"},
                {"ansible_tag": "hana_version", "scope": "component", "component": "hana"},
                {"ansible_tag": "cvers_report", "scope": "component", "component": "abap"},
            ]
        }
        tags = _filter_tags_for_components(
            catalog,
            ["login", "hana_version", "cvers_report"],
            ["hana"],
        )
        self.assertEqual(tags, ["login", "hana_version"])

    def test_customer_filter_builds_customer_group_limit(self):
        args = argparse.Namespace(
            limit=None,
            environment="production",
            customer="RTS",
            component=["hana"],
        )
        self.assertEqual(
            _build_limit(args),
            "environment_production:&customer_rts:&component_hana",
        )

    def test_exact_filters_select_expected_server(self):
        normalized = {
            "servers": {
                "hana-01": {
                    "environment": "Production",
                    "customer": "RTS",
                    "components": ["hana", "host_agent"],
                },
                "app-01": {
                    "environment": "production",
                    "customer": "rts",
                    "components": ["abap", "host_agent"],
                },
            },
            "groups": {},
        }
        args = argparse.Namespace(
            limit=None,
            environment="production",
            customer="RTS",
            component=["hana"],
            target_group=None,
        )
        self.assertEqual(_selected_hosts(normalized, args), ["hana-01"])


if __name__ == "__main__":
    unittest.main()
