"""Config validation: CSV rows, env/dotenv precedence, secret safety."""


import pytest

import config

ENV_KEYS = ("DISCORD_TOKEN", "CHANNELID")


def _clear_env(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class TestSearchCsv:
    def test_good_csv_parses_rows(self, tmp_path):
        path = tmp_path / "rows.csv"
        path.write_text(
            "max_price,friendly_name,search_query\n"
            "30,grigri plush,grigri plush\n"
            "19.99,VHS tape,vhs tape\n",
            encoding="utf-8",
        )
        rows = config.load_search_rows(str(path))
        assert rows == [
            {
                "search_query": "grigri plush",
                "friendly_name": "grigri plush",
                "max_price": "30",
            },
            {
                "search_query": "vhs tape",
                "friendly_name": "VHS tape",
                "max_price": "19.99",
            },
        ]

    def test_missing_csv_named_in_error(self, tmp_path):
        missing = str(tmp_path / "nope.csv")
        with pytest.raises(config.ConfigError, match="search CSV not found"):
            config.load_search_rows(missing)

    def test_missing_headers_suggest_template(self, tmp_path):
        path = tmp_path / "heads.csv"
        path.write_text("max_price,friendly_name\n30,grigri\n", encoding="utf-8")
        with pytest.raises(
            config.ConfigError, match="missing required headers"
        ) as exc:
            config.load_search_rows(str(path))
        message = str(exc.value)
        assert "examples/ebay_deals_to_check.csv" in message
        assert "search_query" in message

    def test_blank_field_line_numbered(self, tmp_path):
        path = tmp_path / "blank.csv"
        path.write_text(
            "max_price,friendly_name,search_query\n"
            "30,,grigri plush\n",
            encoding="utf-8",
        )
        with pytest.raises(
            config.ConfigError, match=r"line 2: blank friendly_name"
        ):
            config.load_search_rows(str(path))

    def test_extra_blank_line_numbered(self, tmp_path):
        path = tmp_path / "blank2.csv"
        path.write_text(
            "max_price,friendly_name,search_query\n"
            "30,name,\n",  # blank query on line 2
            encoding="utf-8",
        )
        with pytest.raises(
            config.ConfigError, match=r"line 2: blank search_query"
        ):
            config.load_search_rows(str(path))

    def test_bad_ceiling_values_rejected(self, tmp_path):
        for bad in ("abc", "NaN", "Infinity", "-1"):
            path = tmp_path / f"bad-{bad}.csv"
            path.write_text(
                f"max_price,friendly_name,search_query\n{bad},n,q\n",
                encoding="utf-8",
            )
            with pytest.raises(config.ConfigError, match="max_price"):
                config.load_search_rows(str(path))

    def test_bom_prefixed_csv_accepted(self, tmp_path):
        path = tmp_path / "bom.csv"
        path.write_text(
            "\ufeffmax_price,friendly_name,search_query\n5,nice,q\n",
            encoding="utf-8",
        )
        rows = config.load_search_rows(str(path))
        assert rows == [
            {
                "search_query": "q",
                "friendly_name": "nice",
                "max_price": "5",
            }
        ]

    def test_invalid_utf8_csv_rejected(self, tmp_path):
        path = tmp_path / "badenc.csv"
        path.write_bytes(
            b"max_price,friendly_name,search_query\n\xff\xfe,n,q\n"
        )
        with pytest.raises(
            config.ConfigError, match="not valid UTF-8"
        ):
            config.load_search_rows(str(path))

    def test_ensure_writable_dir_reports_unusable(self, tmp_path):
        blocked = tmp_path / "ro"
        blocked.mkdir()
        blocked.chmod(0o500)
        with pytest.raises(config.ConfigError, match="cannot create"):
            config.ensure_writable_dir(str(blocked / "sub"))


class TestEnv:
    def test_process_env_wins_over_file(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        dotenv = tmp_path / "both.env"
        dotenv.write_text(
            "DISCORD_TOKEN=from-file\nCHANNELID=111\n", encoding="utf-8"
        )
        monkeypatch.setenv("DISCORD_TOKEN", "from-env")
        monkeypatch.setenv("CHANNELID", "222")
        values = config.load_env(str(dotenv))
        assert values == {"discord_token": "from-env", "channel_id": 222}

    def test_dotenv_fills_when_env_absent(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        dotenv = tmp_path / "one.env"
        dotenv.write_text(
            "DISCORD_TOKEN=from-file\nCHANNELID=333\n", encoding="utf-8"
        )
        values = config.load_env(str(dotenv))
        assert values == {"discord_token": "from-file", "channel_id": 333}

    def test_missing_token_message_no_value(self, tmp_path, monkeypatch):
        _clear_env(monkeypatch)
        dotenv = tmp_path / "chanonly.env"
        dotenv.write_text("CHANNELID=12345\n", encoding="utf-8")
        with pytest.raises(config.ConfigError, match="DISCORD_TOKEN is missing"):
            config.load_env(str(dotenv))

    def test_existing_token_never_echoed_on_channel_error(
        self, tmp_path, monkeypatch
    ):
        _clear_env(monkeypatch)
        secret = "s3cret-token-value"
        monkeypatch.setenv("DISCORD_TOKEN", secret)
        monkeypatch.setenv("CHANNELID", "not-numeric!")
        with pytest.raises(config.ConfigError, match="numeric Discord channel"):
            config.load_env(None)
        # And the success path returns the exact value:
        monkeypatch.setenv("CHANNELID", "98765")
        values = config.load_env(None)
        assert values["discord_token"] == secret
        assert values["channel_id"] == 98765
