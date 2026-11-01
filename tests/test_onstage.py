"""A cola com a infraestrutura da ON: o que dá para verificar sem Supabase.

Este é o pacote com menos cobertura do repositório, e de propósito — ele só faz
chamadas de rede ao Vault e ao Storage, que não têm como ser testadas sem o
projeto real. Mas é também o pacote que **roda no container**: um `import`
quebrado aqui é um deploy que sobe, tenta, e morre no `docker run`.

Então o que se testa é o que não precisa de rede: que o módulo importa, que o
entrypoint existe, que o caminho do PDF segue o layout combinado com o portal, e
que `salvar()` devolve caminho em vez de URL.
"""
import inspect

import pytest


def test_entrypoint_importa_e_tem_main():
    """`CMD ["python", "-m", "onstage.main"]` depende disto.

    Importar `onstage.main` não pode exigir variável de ambiente: a leitura de
    `DATABASE_URL` tem de estar dentro de `main()`, senão o módulo falha no
    import e a mensagem de erro não diz o que falta.
    """
    from onstage import main as entrypoint

    assert callable(entrypoint.main)
    assert hasattr(entrypoint, "Pulso")


def test_nao_existe_servidor_http_no_pacote():
    """A correção central em relação ao executável comprado, como teste.

    O `.exe` subia Flask em `0.0.0.0:5847` sem autenticação, com uma rota que
    devolvia a senha do certificado. Se algum dia alguém adicionar um health
    check HTTP "só para o Railway", é aqui que a conversa acontece — e não
    depois, em produção.

    A verificação é sobre os `import` de verdade, lidos com `ast`, e não sobre o
    texto do arquivo: os docstrings citam Flask justamente para explicar o que
    não se faz, e procurar a palavra solta reprovaria a documentação da decisão.
    """
    import ast
    import pathlib

    PROIBIDOS = {"flask", "waitress", "uvicorn", "fastapi", "starlette",
                 "http.server", "socketserver", "aiohttp", "bottle", "gunicorn"}

    pacote = pathlib.Path(__file__).resolve().parents[1] / "onstage"
    importados: set[str] = set()
    for arquivo in pacote.glob("*.py"):
        for no in ast.walk(ast.parse(arquivo.read_text())):
            if isinstance(no, ast.Import):
                importados.update(a.name for a in no.names)
            elif isinstance(no, ast.ImportFrom) and no.module:
                importados.add(no.module)

    raiz = {nome.split(".")[0] for nome in importados}
    assert not (PROIBIDOS & (importados | raiz)), (
        f"servidor HTTP importado no worker: {PROIBIDOS & (importados | raiz)} — "
        "a saúde é publicada em integra_worker_pulso, não por porta"
    )


def test_pulso_bate_em_tabela_e_nao_em_porta():
    from onstage.main import INTERVALO_PULSO_S, Pulso

    assert "integra_worker_pulso" in inspect.getsource(Pulso)
    # 30 s contra a janela que o painel usa para dizer "vivo". Pulso mais lento
    # que a janela faz worker saudável aparecer como caído.
    assert INTERVALO_PULSO_S <= 60


def test_storage_devolve_caminho_nunca_url(monkeypatch):
    """URL assinada em coluna de banco é vazamento com data marcada.

    O link de 5 min é gerado no portal, na hora de exibir. Aqui só o caminho
    sobe para o banco — é o que permite trocar de bucket ou encurtar a validade
    do link sem reescrever o que já está gravado.
    """
    import httpx

    from onstage.armazenamento import StorageSupabase

    enviados = {}

    def post_falso(url, content=None, headers=None, timeout=None):
        enviados["url"] = url
        enviados["headers"] = headers or {}
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post_falso)
    s = StorageSupabase(supabase_url="https://projeto.supabase.co", service_key="chave")
    caminho = s.salvar("11222333000181/sitfis/202611/x.pdf", b"%PDF-1.4")

    assert caminho == "11222333000181/sitfis/202611/x.pdf"
    assert not caminho.startswith("http"), "o banco guarda caminho, não URL"
    assert "/integra/11222333000181/sitfis/202611/x.pdf" in enviados["url"]
    # `x-upsert` existe porque a mesma consulta pode ser repetida (job
    # reagendado): sem ele, a segunda subida falha em 409 e o job morre por um
    # arquivo que já está lá.
    assert enviados["headers"].get("x-upsert") == "true"


def test_provider_exige_configuracao_explicita(monkeypatch):
    """Sem `DATABASE_URL`/`SUPABASE_URL`, o erro aparece na partida, não no 1º job."""
    from onstage.segredos import ProviderSupabase

    for var in ("DATABASE_URL", "SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(KeyError):
        ProviderSupabase()
