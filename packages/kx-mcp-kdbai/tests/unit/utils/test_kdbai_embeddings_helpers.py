"""get_embedding_config: per database+table lookup against the embeddings CSV."""

import pandas as pd

from kx_mcp_kdbai.utils import embeddings_helpers as mod


_ROW = {
    "database": "mydb", "table": "docs",
    "embedding_provider": "st", "embedding_model": "m",
    "sparse_tokenizer_provider": "st", "sparse_tokenizer_model": "tok",
}


def test_match_returns_providers_and_models(mocker):
    mocker.patch.object(mod, "get_csv_data", return_value=pd.DataFrame([_ROW]))
    assert mod.get_embedding_config("mydb", "docs") == ["st", "m", "st", "tok"]


def test_no_match_returns_nones(mocker):
    row = dict(_ROW, database="other")
    mocker.patch.object(mod, "get_csv_data", return_value=pd.DataFrame([row]))
    assert mod.get_embedding_config("mydb", "docs") == [None, None, None, None]
