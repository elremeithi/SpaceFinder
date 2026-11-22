import os, sys, time, zipfile, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from spacefinder import junk, duplicates, organizer, ai, system_clean


def w(p, data=b""):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "wb") as f:
        f.write(data)


def age(p, days):
    t = time.time() - days * 86400
    os.utime(p, (t, t))


class JunkTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        j = lambda *a: os.path.join(self.d, *a)
        w(j("empty.txt")); w(j("__init__.py"))
        w(j("movie.mp4.crdownload"), b"x" * 100); age(j("movie.mp4.crdownload"), 10)
        w(j("fresh.part"), b"x")                         # too new -> ignored
        w(j("fake.jpg"), b"not a jpeg at all" * 10)
        w(j("trunc.png"), b"\x89PNG\r\n\x1a\n" + b"x" * 500)
        w(j("good.png"), b"\x89PNG\r\n\x1a\n" + b"x" * 500 + b"IEND\xaeB`\x82")
        w(j("zeros.bin"), b"\x00" * 10000)
        w(j("broken.docx"), b"PK garbage")
        zp = j("ok.zip")
        with zipfile.ZipFile(zp, "w") as z: z.writestr("a.txt", "hello")
        w(j("~$report.docx"), b"x" * 160)
        w(j("Thumbs.db"), b"x" * 10)
        os.makedirs(j("emptydir", "nested", "deeper"))
        w(j("onlyjunk", "Thumbs.db"), b"x")
        w(j("real", "keep.txt"), b"data")
        os.makedirs(j("node_modules", "x"))
        self.j = j

    def test_scan(self):
        res = {os.path.relpath(f.path, self.d): f.category for f in junk.scan_junk([self.d])}
        self.assertEqual(res["empty.txt"], junk.CAT_EMPTY_FILE)
        self.assertNotIn("__init__.py", res)
        self.assertEqual(res["movie.mp4.crdownload"], junk.CAT_INCOMPLETE)
        self.assertNotIn("fresh.part", res)
        self.assertEqual(res["fake.jpg"], junk.CAT_CORRUPT)
        self.assertEqual(res["trunc.png"], junk.CAT_CORRUPT)
        self.assertNotIn("good.png", res)
        self.assertEqual(res["zeros.bin"], junk.CAT_ZEROFILLED)
        self.assertEqual(res["broken.docx"], junk.CAT_CORRUPT)
        self.assertNotIn("ok.zip", res)
        self.assertEqual(res["~$report.docx"], junk.CAT_LEFTOVER)
        self.assertEqual(res["Thumbs.db"], junk.CAT_JUNK)
        self.assertEqual(res["emptydir"], junk.CAT_EMPTY_FOLDER)
        self.assertNotIn(os.path.join("emptydir", "nested"), res)
        self.assertEqual(res["onlyjunk"], junk.CAT_EMPTY_FOLDER)
        self.assertNotIn("real", res)
        self.assertFalse(any(k.startswith("node_modules") for k in res))

    def test_smart_review(self):
        fs = junk.scan_junk([self.d])
        for f in fs:
            f.mtime = time.time() - 30 * 86400
        w(self.j("thesis_final.docx"))
        fs += [junk.Finding(self.j("thesis_final.docx"), 0, junk.CAT_EMPTY_FILE, "", mtime=time.time() - 9e6)]
        ai.SmartEngine().review_junk(fs)
        v = {os.path.basename(f.path): f.ai_verdict for f in fs}
        self.assertEqual(v["thesis_final.docx"], "review")
        self.assertEqual(v["empty.txt"], "delete")
        self.assertEqual(v["Thumbs.db"], "delete")
        self.assertEqual(v["fake.jpg"], "review")  # wrong header could be wrong extension


class DupTests(unittest.TestCase):
    def test_dupes(self):
        d = tempfile.mkdtemp()
        big = os.urandom(300 * 1024)
        w(os.path.join(d, "Photos", "a.jpg"), big)
        w(os.path.join(d, "Downloads", "a (1).jpg"), big)
        w(os.path.join(d, "x", "a - copy.jpg"), big)
        w(os.path.join(d, "near.jpg"), big[:-1] + b"Z")   # same size, different tail
        w(os.path.join(d, "s1.txt"), b"same small" * 200)
        w(os.path.join(d, "s2.txt"), b"same small" * 200)
        g = duplicates.find_duplicates([d], min_size=100)
        self.assertEqual(len(g), 2)
        self.assertEqual(len(g[0].files), 3)
        keeper = [f for f in g[0].files if f.keep]
        self.assertEqual(len(keeper), 1)
        self.assertIn("Photos", keeper[0].path)


class OrganizerTests(unittest.TestCase):
    def test_plan_apply_undo(self):
        d = tempfile.mkdtemp()
        os.environ["APPDATA"] = tempfile.mkdtemp()
        os.environ["HOME"] = os.environ["APPDATA"]
        names = ["Screenshot 2024-01-01.png", "Screenshot_20240202.png", "IMG_20230101_1234.jpg", "DSC0001.jpg", "receipt_amazon.pdf", "resume.pdf", "cat.png", "invoice_443.pdf",
                 "setup_x64.exe", "song.mp3", "Acme_report.docx", "Acme_budget.xlsx", "Acme_slides.pptx",
                 "notes.txt", "weird.xyz", "shortcut.lnk"]
        for n in names:
            w(os.path.join(d, n), b"data")
        os.makedirs(os.path.join(d, "Pictures"))
        plan = organizer.build_plan(d)
        self.assertEqual(len(plan), len(names) - 1)  # .lnk skipped
        ai.SmartEngine().categorize(plan, organizer.existing_subfolders(d))
        cat = {p.name: p.category for p in plan}
        self.assertEqual(cat["Screenshot 2024-01-01.png"], "Screenshots")
        self.assertEqual(cat["IMG_20230101_1234.jpg"], "Camera Photos")
        self.assertEqual(cat["cat.png"], "Pictures")       # reused existing folder
        self.assertEqual(cat["invoice_443.pdf"], "Invoices & Receipts")
        self.assertEqual(cat["Acme_report.docx"], "Project - Acme")
        self.assertEqual(cat["song.mp3"], "Music")
        self.assertEqual(cat["weird.xyz"], "Other")
        self.assertEqual(cat["resume.pdf"], "PDFs")  # lone topic -> stays by type
        moved, errs, log = organizer.apply_plan(d, plan)
        self.assertEqual((moved, errs), (len(plan), []))
        self.assertTrue(os.path.exists(os.path.join(d, "Music", "song.mp3")))
        restored, errs = organizer.undo(log)
        self.assertEqual(restored, len(plan))
        self.assertTrue(os.path.exists(os.path.join(d, "song.mp3")))
        self.assertFalse(os.path.exists(os.path.join(d, "Music")))


class SysTests(unittest.TestCase):
    def test_temp_scan_clean(self):
        t = tempfile.mkdtemp()
        os.environ["TEMP"] = t
        w(os.path.join(t, "old.tmp"), b"x" * 50); age(os.path.join(t, "old.tmp"), 5)
        w(os.path.join(t, "sub", "old2.log"), b"x" * 50); age(os.path.join(t, "sub", "old2.log"), 5)
        w(os.path.join(t, "new.tmp"), b"x")
        loc = [l for l in system_clean.default_locations() if l.key == "user_temp"][0]
        system_clean.scan_locations([loc])
        self.assertEqual(len(loc.files), 2)
        n, freed, skipped = system_clean.clean_location(loc)
        self.assertEqual((n, freed), (2, 100))
        self.assertTrue(os.path.exists(os.path.join(t, "new.tmp")))
        self.assertFalse(os.path.exists(os.path.join(t, "sub")))
        self.assertTrue(os.path.exists(t))


class LLMTests(unittest.TestCase):
    def test_rejects_remote_host(self):
        with self.assertRaises(ai.AIError):
            ai.LocalLLM(host="http://example.com:11434")


if __name__ == "__main__":
    unittest.main()
