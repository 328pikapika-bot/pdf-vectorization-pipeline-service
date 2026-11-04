import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from vectorize import chunk_to_row, create_collection, split_document


class SemanticChunkingTests(unittest.TestCase):
    def setUp(self):
        self.text = (
            "# 玉米育种研究\n\n"
            "## 材料与方法\n\n"
            "本研究选择耐密植玉米自交系开展田间试验。试验在三个生态点重复设置，统一记录株高和产量。"
            "通过比较不同密度下的表型表现，筛选稳定高产的育种材料。进一步测定叶片形态、穗位和籽粒含水量，"
            "评估材料在不同环境中的稳定性。\n\n"
            "## 结果\n\n"
            "耐密植材料在高密度条件下保持较高的穗粒数和产量，说明其具有良好的育种利用价值。"
        )
        self.chunks = split_document(
            self.text,
            "maize_breeding.pdf",
            target_chars=48,
            min_chars=20,
            max_chars=70,
            semantic_window=20,
            overlap_ratio=0.15,
        )

    def test_chunks_are_source_aligned_and_have_stable_ids(self):
        self.assertGreaterEqual(len(self.chunks), 3)
        for index, chunk in enumerate(self.chunks):
            self.assertEqual(chunk.id, f"maize_breeding_{index}")
            self.assertEqual(chunk.pdf_index, "maize_breeding.pdf")
            self.assertEqual(chunk.chunk_index, index)
            self.assertEqual(chunk.text, self.text[chunk.char_start:chunk.char_end])
            self.assertTrue(chunk.section_path)

    def test_overlap_is_fifteen_percent_inside_a_section_only(self):
        same_section_pairs = 0
        for previous, current in zip(self.chunks, self.chunks[1:]):
            actual_overlap = max(0, previous.char_end - current.char_start)
            if previous.section_path == current.section_path:
                expected_overlap = max(1, round(len(previous.text) * 0.15))
                self.assertEqual(actual_overlap, expected_overlap)
                self.assertEqual(previous.text[-expected_overlap:], current.text[:expected_overlap])
                same_section_pairs += 1
            else:
                self.assertEqual(actual_overlap, 0)
        self.assertGreaterEqual(same_section_pairs, 1)

    def test_insert_row_contains_only_the_eight_schema_fields(self):
        row = chunk_to_row(self.chunks[0], [0.1, 0.2])
        self.assertEqual(
            set(row),
            {
                "id", "pdf_index", "chunk_index", "text_embed",
                "text", "char_start", "char_end", "section_path",
            },
        )

    def test_reference_section_is_not_vectorized(self):
        text = (
            "# 育种研究\n\n"
            "## 结果\n\n"
            "耐密植材料表现出稳定的高产性。\n\n"
            "## References\n\n"
            "Reference text must not be included in a vector chunk。"
        )
        chunks = split_document(text, "references.pdf", target_chars=80, min_chars=20, max_chars=100)
        self.assertTrue(chunks)
        self.assertTrue(all("Reference text" not in chunk.text for chunk in chunks))

    def test_collection_schema_has_only_the_eight_required_fields(self):
        with patch("vectorize.utility.has_collection", return_value=False), patch("vectorize.Collection") as collection:
            create_collection(None, "semantic_chunks_test", 2)

        schema = collection.call_args_list[0].kwargs["schema"]
        fields = {field.name: field for field in schema.fields}
        self.assertEqual(
            set(fields),
            {
                "id", "pdf_index", "chunk_index", "text_embed",
                "text", "char_start", "char_end", "section_path",
            },
        )
        self.assertTrue(fields["id"].is_primary)
        self.assertEqual(int(fields["text_embed"].params["dim"]), 2)


if __name__ == "__main__":
    unittest.main()
