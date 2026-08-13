from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from inventory_compiler.compiler import CompileError, compile_inventory


class InventoryCompilerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "defaults.json").write_text(
            json.dumps(
                {
                    "connection": {
                        "ssh_port": 22,
                        "python_interpreter": "/usr/bin/python3",
                        "credential_profile": "default",
                    },
                    "inventory": {"host_agent_expected_by_default": True},
                }
            )
        )
        (self.root / "credentials.json").write_text(
            json.dumps({"profiles": {"default": {"ssh_user": "ansible"}}})
        )
        (self.root / "overrides.json").write_text("{}")

    def tearDown(self):
        self.temp.cleanup()

    def compile(self):
        return compile_inventory(
            servers_path=self.root / "servers.csv",
            instances_path=self.root / "instances.csv",
            defaults_path=self.root / "defaults.json",
            credentials_path=self.root / "credentials.json",
            overrides_path=self.root / "overrides.json",
        )

    def test_combined_server_generates_compatibility_groups_and_vars(self):
        (self.root / "servers.csv").write_text(
            "server_id,address,environment,customer,enabled\n"
            "sap-01,10.0.0.1,sandbox,RTS,true\n"
        )
        (self.root / "instances.csv").write_text(
            "server_id,component,sid,instance_number,userstore_key\n"
            "sap-01,hana,HDB,02,HDBKEY\n"
            "sap-01,abap,S4H,00,DEFAULT\n"
            "sap-01,ascs,S4H,01,\n"
        )
        inventory, normalized = self.compile()
        host = inventory["all"]["hosts"]["sap-01"]
        self.assertEqual(host["hana_sid"], "HDB")
        self.assertEqual(host["app_sid"], "S4H")
        self.assertEqual(host["app_ascs_instance_number"], "01")
        self.assertIn("sap-01", normalized["groups"]["hana_db"])
        self.assertIn("sap-01", normalized["groups"]["nw_app"])
        self.assertIn("sap-01", normalized["groups"]["component_hana"])
        self.assertIn("sap-01", normalized["groups"]["component_abap"])
        self.assertIn("sap-01", normalized["groups"]["customer_rts"])
        self.assertEqual(normalized["servers"]["sap-01"]["customer"], "RTS")

    def test_compiles_one_thousand_servers(self):
        with (self.root / "servers.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["server_id", "address", "environment", "customer", "enabled"])
            for index in range(1000):
                writer.writerow([f"hana-{index:04d}", f"10.0.{index // 250}.{index % 250 + 1}", "production", "RTS", "true"])
        with (self.root / "instances.csv").open("w", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["server_id", "component", "sid", "instance_number"])
            for index in range(1000):
                writer.writerow([f"hana-{index:04d}", "hana", "HDB", "02"])
        inventory, normalized = self.compile()
        self.assertEqual(len(inventory["all"]["hosts"]), 1000)
        self.assertEqual(len(normalized["groups"]["component_hana"]), 1000)

    def test_duplicate_server_id_is_rejected(self):
        (self.root / "servers.csv").write_text(
            "server_id,address,enabled\n"
            "duplicate,10.0.0.1,true\n"
            "duplicate,10.0.0.2,true\n"
        )
        (self.root / "instances.csv").write_text("server_id,component,sid,instance_number\n")
        with self.assertRaises(CompileError):
            self.compile()


if __name__ == "__main__":
    unittest.main()
