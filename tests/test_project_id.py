"""project_id 的口径测试：分区标识的格式、稳定性、符号链接归一与名称保留。"""
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "skills", "_shared"))
import project_id  # noqa: E402


class ProjectSlugTest(unittest.TestCase):
    def test_format_and_trailing_separator_stability(self):
        base = tempfile.mkdtemp()
        path = os.path.join(base, "demo")
        os.makedirs(path)
        slug = project_id.project_slug(path)
        self.assertRegex(slug, r"^demo-[0-9a-f]{6}$")
        self.assertEqual(slug, project_id.project_slug(path + os.sep))

    def test_symlink_resolves_to_same_partition(self):
        base = tempfile.mkdtemp()
        target = os.path.join(base, "real")
        link = os.path.join(base, "link")
        os.makedirs(target)
        os.symlink(target, link)
        self.assertEqual(project_id.project_slug(link), project_id.project_slug(target))

    def test_case_and_non_ascii_names_are_preserved(self):
        # 不创建真实目录：大小写不同的同级目录在大小写不敏感的文件系统上无法共存，
        # 而 project_slug 对不存在的路径同样有效
        upper = project_id.project_slug("/opt/workspace/Demo")
        lower = project_id.project_slug("/opt/workspace/demo")
        cjk = project_id.project_slug("/opt/workspace/演示项目")
        self.assertNotEqual(upper, lower)
        self.assertTrue(upper.startswith("Demo-"))
        self.assertTrue(lower.startswith("demo-"))
        self.assertTrue(cjk.startswith("演示项目-"))

    def test_empty_path_returns_empty_slug(self):
        self.assertEqual(project_id.project_slug(""), "")
        self.assertEqual(project_id.project_slug("   "), "")


if __name__ == "__main__":
    unittest.main()
