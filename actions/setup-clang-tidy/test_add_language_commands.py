#!/usr/bin/env python3
"""Unit tests for add_language_commands."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import add_language_commands as alc  # noqa: E402


class TestBaseCommand(unittest.TestCase):
    def test_prefers_a_cxx_entry_over_whatever_is_first(self):
        # A database can lead with the C compiler; the flags we copy have to
        # come from a C++ entry or the generated command is a C one.
        db = [{"file": "/p/a.c", "command": "/bin/clang -DC_ONLY -c /p/a.c"},
              {"file": "/p/b.cpp",
               "command": "/bin/clang++ -DCXX -I/p/inc -std=c++20 -c /p/b.cpp"}]
        self.assertEqual(alc.base_command(db),
                         ["/bin/clang++", "-DCXX", "-I/p/inc", "-std=c++20"])

    def test_drops_the_input_and_output(self):
        db = [{"file": "/p/b.cpp",
               "command": "/bin/clang++ -I/p -c /p/b.cpp -o /p/b.o"}]
        self.assertEqual(alc.base_command(db), ["/bin/clang++", "-I/p"])

    def test_reads_the_arguments_form_too(self):
        db = [{"file": "/p/b.cpp",
               "arguments": ["/bin/clang++", "-I/p", "-c", "/p/b.cpp"]}]
        self.assertEqual(alc.base_command(db), ["/bin/clang++", "-I/p"])

    def test_keeps_the_value_of_a_separated_flag(self):
        # -isystem's value is the next argument; dropping it is how a
        # project's own include path goes missing.
        db = [{"file": "/p/b.cpp",
               "command": "/bin/clang++ -isystem /p/include -I/q -c /p/b.cpp"}]
        self.assertEqual(alc.base_command(db),
                         ["/bin/clang++", "-isystem", "/p/include", "-I/q"])

    def test_no_cxx_entry_is_an_error_not_a_wrong_answer(self):
        with self.assertRaises(SystemExit):
            alc.base_command([{"file": "/p/a.c", "command": "/bin/clang -c /p/a.c"}])


class TestPrelude(unittest.TestCase):
    def setUp(self):
        self.dir = Path(__file__).resolve().parent / "_t"
        self.dir.mkdir(exist_ok=True)

    def tearDown(self):
        for f in self.dir.iterdir():
            f.unlink()
        self.dir.rmdir()

    def test_a_header_including_its_own_needs_no_prelude(self):
        p = self.dir / "self.cuh"
        p.write_text('#include "project/config.h"\n__device__ void f();\n')
        self.assertFalse(alc.needs_prelude(p))

    def test_a_header_including_nothing_of_its_own_needs_one(self):
        p = self.dir / "bare.h"
        p.write_text("#include <thrust/reduce.h>\nCUDA_HOST_DEVICE void f();\n")
        self.assertTrue(alc.needs_prelude(p))


class TestAddCommands(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / "_r"
        self.root.mkdir(exist_ok=True)
        (self.root / "k.cu").write_text("__global__ void k() {}\n")
        (self.root / "plain.h").write_text("int f();\n")
        # Both must be ignored: a build tree is anything with a
        # CMakeCache.txt, configured or left over.
        for stale in ("b", "build-old"):
            (self.root / stale).mkdir(exist_ok=True)
            (self.root / stale / "CMakeCache.txt").write_text("")
            (self.root / stale / "generated.cu").write_text("__global__ void g(){}\n")

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.root)], check=True)

    def _db(self):
        return [{"file": "/p/b.cpp",
                 "command": "/bin/clang++ -I/p -std=c++17 -c /p/b.cpp"}]

    def test_adds_cuda_and_leaves_plain_headers_alone(self):
        db = self._db()
        added = alc.add_commands(db, self.root / "b", self.root,
                                 {"CUDA_PATH": "/cuda"}, [])
        self.assertEqual(added["cuda"], 1)
        entry = db[-1]
        self.assertTrue(entry["file"].endswith("k.cu"))
        # Inherited from the C++ entry, not invented.
        self.assertIn("-I/p", entry["arguments"])
        self.assertIn("-std=c++17", entry["arguments"])
        self.assertIn("--cuda-path=/cuda", entry["arguments"])

    def test_without_the_env_the_language_is_skipped(self):
        db = self._db()
        self.assertEqual(
            alc.add_commands(db, self.root / "b", self.root, {}, []), {})
        self.assertEqual(len(db), 1)


if __name__ == "__main__":
    unittest.main()


class TestMultipleLanguages(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / "_m"
        self.root.mkdir(exist_ok=True)
        # One header in both languages, as Differentiator.h is.
        (self.root / "both.h").write_text(
            '#include "own.h"\n__device__ void d();\n#pragma omp parallel for\n')
        (self.root / "own.h").write_text("int f();\n")

    def tearDown(self):
        subprocess.run(["rm", "-rf", str(self.root)], check=True)

    def test_one_entry_carrying_both_sets_of_flags(self):
        db = [{"file": "/p/b.cpp",
               "command": "/bin/clang++ -I/p -std=c++17 -c /p/b.cpp"}]
        added = alc.add_commands(db, self.root / "b", self.root,
                                 {"CUDA_PATH": "/cuda", "CLANG_TIDY_OPENMP": "1"},
                                 [])
        self.assertEqual(added, {"cuda": 1, "openmp": 1})
        entries = [e for e in db if e["file"].endswith("both.h")]
        # One entry, not one per language: clang-tidy reads only the first.
        self.assertEqual(len(entries), 1)
        self.assertIn("-xcuda", entries[0]["arguments"])
        self.assertIn("-fopenmp", entries[0]["arguments"])

    def test_openmp_alone_does_not_pull_in_cuda_flags(self):
        db = [{"file": "/p/b.cpp",
               "command": "/bin/clang++ -c /p/b.cpp"}]
        alc.add_commands(db, self.root / "b", self.root,
                         {"CLANG_TIDY_OPENMP": "1"}, [])
        args = [e for e in db if e["file"].endswith("both.h")][0]["arguments"]
        self.assertIn("-fopenmp", args)
        self.assertNotIn("-xcuda", args)

    def test_a_header_without_a_language_flag_is_told_it_is_cxx(self):
        # clang reads a bare .h as C; without -xc++ every include fails.
        db = [{"file": "/p/b.cpp", "command": "/bin/clang++ -c /p/b.cpp"}]
        alc.add_commands(db, self.root / "b", self.root,
                         {"CLANG_TIDY_OPENMP": "1"}, [])
        args = [e for e in db if e["file"].endswith("both.h")][0]["arguments"]
        self.assertIn("-xc++", args)

    def test_cuda_supplies_its_own_language_and_is_not_overridden(self):
        db = [{"file": "/p/b.cpp", "command": "/bin/clang++ -c /p/b.cpp"}]
        alc.add_commands(db, self.root / "b", self.root,
                         {"CUDA_PATH": "/cuda"}, [])
        args = [e for e in db if e["file"].endswith("both.h")][0]["arguments"]
        self.assertIn("-xcuda", args)
        self.assertNotIn("-xc++", args)

    def test_a_file_the_build_already_compiles_is_left_alone(self):
        # CMake's own command is the right one; a second entry for the same
        # file would shadow it, since clang-tidy reads only the first.
        src = str((self.root / "both.h").resolve())
        db = [{"file": "/p/b.cpp", "command": "/bin/clang++ -c /p/b.cpp"},
              {"file": src, "command": "/bin/clang++ -DFROM_CMAKE -c " + src}]
        added = alc.add_commands(db, self.root / "b", self.root,
                                 {"CLANG_TIDY_OPENMP": "1"}, [])
        self.assertEqual(added["openmp"], 0)
        self.assertEqual([e for e in db if e["file"] == src][0]["command"],
                         "/bin/clang++ -DFROM_CMAKE -c " + src)

    def test_a_header_that_only_avoids_openmp_is_not_selected(self):
        (self.root / "avoids.h").write_text(
            '#include "own.h"\n#ifndef _OPENMP\nint serial();\n#endif\n')
        db = [{"file": "/p/b.cpp", "command": "/bin/clang++ -c /p/b.cpp"}]
        alc.add_commands(db, self.root / "b", self.root,
                         {"CLANG_TIDY_OPENMP": "1"}, [])
        self.assertEqual([e for e in db if e["file"].endswith("avoids.h")], [])
