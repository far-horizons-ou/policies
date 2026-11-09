"""Exercise the exact workflow script using throwaway Git repositories."""
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from textwrap import dedent
import unittest

WORKFLOW = Path(__file__).resolve().parents[1] / "workflows/database-change-gate.yml"
SCRIPT = dedent(WORKFLOW.read_text(encoding="utf-8").split("        run: |\n", 1)[1])


class DatabaseGateTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet", "-b", "main")
        self.git("config", "user.email", "fixtures@example.invalid")
        self.git("config", "user.name", "Fixture")
        self.base = self.commit({"README.md": "Initial fixture\n"})
        self.git("switch", "-qc", "change")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo).strip().decode()

    def commit(self, files):
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if content is None:
                path.unlink()
            else:
                path.write_text(content, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "--quiet", "-m", "fixture")
        return self.git("rev-parse", "HEAD")

    def run_gate(self, kind, event, head):
        event_path = self.root / "event.json"
        event_path.write_text(json.dumps(event), encoding="utf-8")
        config = self.root / "gitconfig"
        config.write_text(
            f'[url "{self.repo}"]\n\tinsteadOf = https://github.com/fixture/repo.git\n',
            encoding="utf-8",
        )
        return subprocess.run(
            [sys.executable, "-c", SCRIPT],
            env={
                **os.environ,
                "GIT_CONFIG_GLOBAL": str(config),
                "GITHUB_EVENT_PATH": str(event_path),
                "GITHUB_EVENT_NAME": kind,
                "GITHUB_REPOSITORY": "fixture/repo",
                "GITHUB_SHA": head,
                "READ_TOKEN": "fixture-no-credentials",
            },
            capture_output=True,
            text=True,
            check=False,
        )

    def group(self, head, base=None):
        return self.run_gate("merge_group", {"merge_group": {
            "base_sha": base or self.base, "base_ref": "refs/heads/main", "head_sha": head,
        }}, head)

    def pull_request(self, head, merge_head):
        return self.run_gate("pull_request", {"pull_request": {
            "base": {"sha": self.base, "ref": "main"}, "head": {"sha": head},
        }}, merge_head)

    def merge_result(self):
        self.git("switch", "-qc", "synthetic-merge", "main")
        self.git("merge", "--quiet", "--no-ff", "--no-edit", "change")
        return self.git("rev-parse", "HEAD")

    def check_flag(self, result, flag):
        self.assertEqual(result.returncode, int(flag != "false"), result.stdout + result.stderr)
        self.assertIn(f"database_change={flag}\n", result.stdout)

    def test_pull_request_allows_docs_mock_tests_and_ordinary_ui(self):
        head = self.commit({
            "docs/database.md": "Use psql to describe SELECT x FROM users.\n",
            "src/database.test.ts": "const db = { query: vi.fn() }; db.query();\n",
            "src/filter.ts": "export const options = ['database', 'select', 'delete'];\n",
            "src/schema.graphql": "type Product { name: String }\n",
        })
        self.check_flag(self.pull_request(head, self.merge_result()), "false")

    def test_pull_request_rejects_schema_even_with_docs_and_a_green_lookalike(self):
        head = self.commit({
            "prisma/schema.prisma": "model Listing { id Int @id }\n",
            "docs/database.md": "The schema changed.\n",
            ".github/workflows/fake.yml": "jobs: {database-change-gate: {if: false}}\n",
        })
        result = self.pull_request(head, self.merge_result())
        self.check_flag(result, "true")
        self.assertIn('"prisma/schema.prisma"', result.stdout)

    def test_merge_group_rejects_database_changes_across_commits(self):
        first = self.commit({"README.md": "Benign update\n"})
        self.check_flag(self.group(first), "false")
        last = self.commit({
            "packages/catalog/migrations-pg/100.sql": "alter table listings add column test int;\n",
            "src/route.ts": "await prisma.listing.findUnique({ where: { id } });\n",
            "src/worker.ts": "await env.CATALOG.prepare(statement).run();\n",
            "src/other.ts": "const sql = 'select id from listings';\n",
            "compose.yml": "services: { db: { image: postgres:17 } }\n",
            "wrangler.jsonc": '{"d1_databases": []}\n',
            ".env.example": "DATABASE_URL=\n",
            ".github/workflows/backup.yml": "steps: [{run: pg_dump}]\n",
            "infra/db.tf": 'resource "digitalocean_database_connection_pool" "fixture" {}\n',
            "test/integration.ts": "import pg from 'pg'; new pg.Client();\n",
            "scripts/backup.py": "import sqlite3\n",
            "src/meridian.ts": "import { closeAllPools } from '@example/db-client';\n",
        })
        result = self.group(last)
        self.check_flag(result, "true")
        self.assertEqual(result.stdout.count("database:"), 12, result.stdout)

    def test_deletion_and_rename_are_checked_on_the_old_side(self):
        self.git("switch", "-q", "main")
        self.base = self.commit({"prisma/old.ts": "export const config = 1;\n",
                                 "script.sh": "pg_restore fixture.dump\n"})
        self.git("switch", "-qC", "change", "main")
        head = self.commit({"prisma/old.ts": None, "misc/new.ts": "export const config = 1;\n",
                            "script.sh": "printf safe\n"})
        result = self.group(head)
        self.check_flag(result, "true")
        self.assertIn('"prisma/old.ts"', result.stdout)
        self.assertIn('"script.sh"', result.stdout)

    def test_lockfile_matches_a_changed_driver_without_blanket_lockfile_rejection(self):
        self.git("switch", "-q", "main")
        self.base = self.commit({"pnpm-lock.yaml": "pg: 8.0\nother: 1.0\n\n\nunrelated: 1.0\n"})
        self.git("switch", "-qC", "change", "main")
        head = self.commit({"pnpm-lock.yaml": "pg: 8.0\nother: 1.0\n\n\nunrelated: 2.0\n"})
        self.check_flag(self.group(head), "false")
        head = self.commit({"pnpm-lock.yaml": "pg: 8.1\nother: 1.0\n\n\nunrelated: 2.0\n"})
        self.check_flag(self.group(head), "true")

    def test_unknown_and_missing_diff_fail_closed(self):
        head = self.commit({"data.opaque": "unclassified\n"})
        self.check_flag(self.group(head), "unknown")
        self.check_flag(self.group("a" * 40), "unknown")
        self.check_flag(self.run_gate("merge_group", {"merge_group": {}}, head), "unknown")

    def test_stale_base_and_wrong_pull_request_parent_fail_closed(self):
        head = self.commit({"README.md": "ordinary change\n"})
        merge_head = self.merge_result()
        self.check_flag(self.pull_request("a" * 40, merge_head), "unknown")
        self.git("switch", "-q", "main")
        self.commit({"base-only.txt": "base advanced\n"})
        self.check_flag(self.group(head), "unknown")
        self.check_flag(self.pull_request(head, merge_head), "unknown")

    def test_repository_wrapper_and_orm_helper_are_flagged(self):
        head = self.commit({
            "src/handler.ts": "import { usersRepository } from './repositories/users';\n"
                              "await usersRepository.remove(id);\n",
            "src/soft-delete.ts": "export function softDeleteFilterArgs() { return { changed: true }; }\n",
            "src/service.ts": "await basePrisma.$executeRaw`UPDATE users SET active = false`;\n",
        })
        result = self.group(head)
        self.check_flag(result, "true")
        self.assertEqual(result.stdout.count("database:"), 3, result.stdout)

    def test_unclassified_repository_implementation_is_unknown(self):
        head = self.commit({"src/repositories/users.ts": "export const remove = () => invoke(5);\n"})
        self.check_flag(self.group(head), "unknown")

    def test_multiline_sql_where_change_is_flagged(self):
        self.git("switch", "-q", "main")
        before = "export const query = `\nselect id\nfrom listings\nwhere visible = true\n`;\n"
        self.base = self.commit({"src/listings.ts": before})
        self.git("switch", "-qC", "change", "main")
        head = self.commit({"src/listings.ts": before.replace("= true", "= false")})
        self.check_flag(self.group(head), "true")

    def test_lifecycle_hook_is_unknown_and_ordinary_manifest_metadata_is_allowed(self):
        self.git("switch", "-q", "main")
        self.base = self.commit({
            "package.json": '{"name":"app","scripts":{"start":"node server.js"}}\n',
            "scripts/backfill.js": "export const job = () => db.query('DELETE FROM archived');\n",
        })
        self.git("switch", "-qC", "change", "main")
        metadata = '{"name":"app","description":"ordinary","scripts":{"start":"node server.js"}}\n'
        head = self.commit({"package.json": metadata})
        self.check_flag(self.group(head), "false")
        head = self.commit({"package.json": metadata.replace(
            '"start":', '"prestart":"node scripts/backfill.js","start":'
        )})
        self.check_flag(self.group(head), "unknown")

    def test_route_copy_is_allowed_and_query_argument_change_is_flagged(self):
        self.git("switch", "-q", "main")
        before = (
            "import { prisma } from '@/db';\n"
            "const listing = await prisma.listing.findUnique({\n"
            "    where: { active: true },\n"
            "});\n"
            "return new Response('Listing available');\n"
        )
        self.base = self.commit({"src/route.ts": before})
        self.git("switch", "-qC", "change", "main")
        head = self.commit({"src/route.ts": before.replace("Listing available", "Ready")})
        self.check_flag(self.group(head), "false")
        head = self.commit({"src/route.ts": before.replace("active: true", "active: false")})
        self.check_flag(self.group(head), "true")

    def test_workflow_with_indirect_command_is_unknown(self):
        head = self.commit({".github/workflows/deploy.yml": "steps: [{run: make release}]\n"})
        result = self.group(head)
        self.check_flag(result, "unknown")
        self.assertIn("can delegate", result.stdout)

    def test_symlink_and_binary_source_fail_closed(self):
        (self.repo / "escape.md").symlink_to("README.md")
        (self.repo / "opaque.ts").write_bytes(b"\0opaque")
        head = self.commit({})
        result = self.group(head)
        self.check_flag(result, "unknown")
        self.assertIn("symlink", result.stdout)
        self.assertIn("binary content", result.stdout)


if __name__ == "__main__":
    unittest.main()
