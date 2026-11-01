"""Exceções do pacote. Todas herdam de IntegraErro."""


class IntegraErro(Exception):
    """Erro base."""


class ConfiguracaoIncompleta(IntegraErro):
    """Falta certificado, senha, consumer key/secret ou CNPJ do contratante."""


class CertificadoVencido(IntegraErro):
    """O certificado A1 está fora da validade."""


class FalhaAutenticacao(IntegraErro):
    """O endpoint /authenticate do SERPRO recusou as credenciais."""


class ErroSerpro(IntegraErro):
    """Resposta de erro do gateway Integra Contador."""

    def __init__(self, status_http: int, mensagens: list[dict], corpo: str = ""):
        self.status_http = status_http
        self.mensagens = mensagens
        self.corpo = corpo[:2000]
        texto = "; ".join(
            f"{m.get('codigo', '')} {m.get('texto', '')}".strip() for m in mensagens
        ) or self.corpo[:300]
        super().__init__(f"SERPRO HTTP {status_http}: {texto}")


class AindaProcessando(IntegraErro):
    """O SERPRO ainda não terminou (ex.: relatório SITFIS com HTTP 204)."""

    def __init__(self, mensagem: str = "Ainda em processamento no SERPRO", espera_segundos: int | None = None):
        self.espera_segundos = espera_segundos
        super().__init__(mensagem)
