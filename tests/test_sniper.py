import app.sniper as sniper


def test_build_clients_dry_run_uses_mock(monkeypatch):
    monkeypatch.setattr(sniper.settings, "sniper_dry_run", True)
    monkeypatch.setattr(sniper.settings, "dynadot_api_key", "")
    monkeypatch.setattr(sniper.settings, "namejet_api_key", "")
    monkeypatch.setattr(sniper.settings, "namejet_api_secret", "")

    clients = sniper._build_registrar_clients()

    assert len(clients) == 1
    assert isinstance(clients[0], sniper.MockRegistrarClient)


def test_build_clients_empty_without_keys(monkeypatch):
    monkeypatch.setattr(sniper.settings, "sniper_dry_run", False)
    monkeypatch.setattr(sniper.settings, "dynadot_api_key", "")
    monkeypatch.setattr(sniper.settings, "namejet_api_key", "")
    monkeypatch.setattr(sniper.settings, "namejet_api_secret", "")

    clients = sniper._build_registrar_clients()

    assert clients == []


def test_build_clients_uses_configured_registrars(monkeypatch):
    monkeypatch.setattr(sniper.settings, "sniper_dry_run", False)
    monkeypatch.setattr(sniper.settings, "dynadot_api_key", "dynadot-key")
    monkeypatch.setattr(sniper.settings, "namejet_api_key", "namejet-key")
    monkeypatch.setattr(sniper.settings, "namejet_api_secret", "namejet-secret")

    clients = sniper._build_registrar_clients()

    assert len(clients) == 2
    assert isinstance(clients[0], sniper.DynadotClient)
    assert isinstance(clients[1], sniper.NamejetClient)
