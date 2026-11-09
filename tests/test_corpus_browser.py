"""Browser candidate capture and dataset version selection against real HTTP."""

import json
import os
import unittest

from app.platform.review_dataset import load_frozen
from app.scripts.demo_stack import DemoStack


@unittest.skipUnless(os.environ.get("RUN_BROWSER_E2E") == "1", "Enable Chromium tests")
class CorpusBrowserTests(unittest.TestCase):
    def test_capture_curate_freeze_and_open_version(self):
        from playwright.sync_api import expect, sync_playwright

        with DemoStack() as stack, sync_playwright() as p:
            run, _ = stack.run("手机号 13800138000 订单查询失败", session="capture")
            stack.completed(run["run_id"])
            browser = p.chromium.launch()
            page = browser.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(stack.url + "/review")
            page.locator("#reviewToken").fill("demo-admin")
            page.locator("#datasetManagement summary").click()
            page.locator("#captureRun").fill(run["run_id"])
            page.locator("#captureCandidate").click()
            expect(page.locator("#candidateList article")).to_have_count(1)
            self.assertNotIn(
                "13800138000",
                page.locator("#candidateList textarea").first.input_value(),
            )
            page.get_by_label("候选整理说明").fill(
                "自动化测试夹具：核对脱敏，不代表真实人工评审"
            )
            page.get_by_text("保存整理结果", exact=True).click()
            expect(page.locator("#reviewStatus")).to_contain_text("已整理候选")
            candidate = stack.request(
                "GET", "/v1/admin/evaluation/candidates", admin=True
            ).json()["candidates"][0]
            _, docs, cases = load_frozen()
            case = cases[0]
            case.update(
                query=candidate["query"],
                candidate_id=candidate["candidate_id"],
                candidate_revision=candidate["revision"],
                source_hash=candidate["source_hash"],
            )
            document = {
                "name": "browser-fixture",
                "version": "1",
                "documents": docs,
                "cases": [case],
                "candidate_ids": [candidate["candidate_id"]],
            }
            page.locator("#datasetFile").set_input_files(
                {
                    "name": "dataset.json",
                    "mimeType": "application/json",
                    "buffer": json.dumps(document).encode(),
                }
            )
            page.locator("#freezeDataset").click()
            expect(page.locator("#reviewStatus")).to_contain_text("新版本已冻结")
            page.locator("#openReview").click()
            expect(page.locator("#progress")).to_contain_text("0 / 1")
            page.locator("#listDatasets").click()
            expect(page.locator("#datasetList")).to_contain_text("browser-fixture / 1")
            self.assertFalse(errors, errors)
            browser.close()
