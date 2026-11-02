"""Review labels cannot silently become approved evidence or unfreeze holdout."""

import json
import tempfile
import unittest
from pathlib import Path

from app.platform.common import PlatformError
from app.platform.evaluation import validate_dataset
from app.scripts.review_evaluation import (
    ROOT,
    apply_review,
    load_frozen,
    offline_report,
    review_template,
)


class ReviewEvaluationTests(unittest.TestCase):
    def test_frozen_corpus_has_disjoint_families_and_pending_review(self):
        manifest, _, cases = load_frozen()
        self.assertEqual(len(cases), 32)
        self.assertTrue(validate_dataset(cases))
        review = review_template(manifest, cases)
        with self.assertRaisesRegex(PlatformError, "REVIEW_INCOMPLETE"):
            apply_review(manifest, cases, review)
        with self.assertRaisesRegex(PlatformError, "HOLDOUT_REVIEW_REQUIRED"):
            offline_report("holdout")

    def test_modified_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("manifest.json", "documents.json", "cases.json"):
                (root / name).write_bytes((ROOT / name).read_bytes())
            (root / "cases.json").write_text("[]")
            with self.assertRaisesRegex(PlatformError, "FROZEN_DATASET_CHANGED"):
                load_frozen(root)

    def test_review_hash_and_duplicate_rows_rejected(self):
        manifest, _, cases = load_frozen()
        review = review_template(manifest, cases)
        review["dataset_sha256"] = "wrong"
        with self.assertRaisesRegex(PlatformError, "REVIEW_DATASET_MISMATCH"):
            apply_review(manifest, cases, review)
        review = review_template(manifest, cases)
        review["reviews"][1] = json.loads(json.dumps(review["reviews"][0]))
        with self.assertRaisesRegex(PlatformError, "REVIEW_INCOMPLETE"):
            apply_review(manifest, cases, review)
