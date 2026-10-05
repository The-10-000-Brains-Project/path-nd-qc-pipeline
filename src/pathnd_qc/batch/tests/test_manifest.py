"""Check that discovery deduplicates symlink aliases of the same slide."""
import csv
from pathlib import Path
import tempfile
import unittest

from pathnd_qc.batch.manifest import build_jobs, discover


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.slides = self.root / "slides"
        self.slides.mkdir()
        self.slide = self.slides / "a.svs"
        self.slide.touch()
        self.alias = self.root / "linked-slides"
        self.alias.symlink_to(self.slides, target_is_directory=True)

    def manifest(self, rows):
        path = self.root / "manifest.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["slide", "stain", "bank", "tissue_mask"])
            writer.writerows(rows)
        return str(path)

    def test_directory_and_manifest_aliases_merge_metadata(self):
        mask = self.slides / "mask.png"
        mask.touch()
        path = self.manifest([
            [self.alias / "a.svs", "Hirano", "", self.alias / "mask.png"],
            [self.slide, "", "PART", mask],
        ])
        for directory in (self.slides, self.alias):
            with self.subTest(directory=directory):
                jobs = build_jobs(discover(slides_file=path, slide_dir=str(directory)))
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0].slide, str(self.slide))
                self.assertEqual(jobs[0].stain, "Hirano")
                self.assertEqual(jobs[0].bank, "PART")
                self.assertEqual(jobs[0].supplied, {"tissue_mask": str(mask)})
                self.assertEqual(jobs[0].problems, [])

    def test_manifest_alias_conflicts_refuse_one_job(self):
        path = self.manifest([
            [self.slide, "Hirano", "", ""],
            [self.alias / "a.svs", "AT8", "", ""],
        ])
        jobs = build_jobs(discover(slides_file=path))
        self.assertEqual(len(jobs), 1)
        self.assertEqual(len(jobs[0].problems), 1)
        self.assertIn("conflicting manifest values for stain", jobs[0].problems[0])
        self.assertIn("Hirano", jobs[0].problems[0])
        self.assertIn("AT8", jobs[0].problems[0])

    def test_list_deduplicates_directory_and_file_symlinks(self):
        file_alias = self.root / "renamed.svs"
        file_alias.symlink_to(self.slide)
        path = self.root / "slides.txt"
        path.write_text(f"{self.slide}\n{self.alias / 'a.svs'}\n{file_alias}\n")
        self.assertEqual(discover(slides_file=str(path)), [{"slide": str(self.slide)}])

    def test_distinct_files_with_same_basename_remain_separate(self):
        other = self.root / "other"
        other.mkdir()
        slide = other / "a.svs"
        slide.touch()
        path = self.manifest([[self.slide, "", "", ""], [slide, "", "", ""]])
        jobs = build_jobs(discover(slides_file=path))
        self.assertEqual({job.slide for job in jobs}, {str(self.slide), str(slide)})
        self.assertEqual(len({job.key for job in jobs}), 2)

    def test_remote_uris_keep_their_identity(self):
        uris = ["gs://bank-a/slides/a.svs", "gs://bank-b/slides/a.svs"]
        path = self.manifest([[uri, "", "", ""] for uri in uris + uris[:1]])
        self.assertEqual(discover(slides_file=path), [{"slide": uri} for uri in uris])


if __name__ == "__main__":
    unittest.main()
