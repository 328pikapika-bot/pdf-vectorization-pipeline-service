"""Critical dump decoding cases: NULL, escapes and SQL-as-data boundaries."""
import importlib.util
import pathlib
import unittest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / 'scripts' / 'enrich_milvus_metadata.py'
SPEC = importlib.util.spec_from_file_location('enrich_metadata', SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class DumpParserTests(unittest.TestCase):
    def parse(self, values):
        return MODULE.parse_values('INSERT INTO `article_sciwatch_dandan` VALUES (' + values + ');\n')

    def test_null_empty_literal_and_unicode(self):
        values = ["'article-id'", "'水稻, 育种'", 'NULL', "'NULL'", "''"] + ['NULL'] * 15
        result = self.parse(', '.join(values))
        self.assertEqual(result['title'], '水稻, 育种')
        self.assertIsNone(result['year'])
        self.assertEqual(result['volume'], 'NULL')
        self.assertEqual(result['issue'], '')

    def test_mysql_backslash_and_quote_escaping(self):
        title = r"'Author\'s \\ path\nnext\tcolumn\rreturn\0\Z\%\_ doubled '' quote'"
        result = self.parse(', '.join(["'id'", title] + ['NULL'] * 18))
        self.assertEqual(result['title'], "Author's \\ path\nnext\tcolumn\rreturn\0\x1a\\%\\_ doubled ' quote")

    def test_refuse_sql_commands_or_wrong_column_count(self):
        with self.assertRaises(AssertionError):
            MODULE.parse_values('DROP TABLE IF EXISTS `article_sciwatch_dandan`;')
        with self.assertRaises(AssertionError):
            self.parse("'one-column'")
        with self.assertRaises(ValueError):
            self.parse("'unclosed")


if __name__ == '__main__':
    unittest.main()
