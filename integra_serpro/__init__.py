"""Cliente da API Integra Contador (SERPRO) para o portal ON Soluções."""
from .catalogo import MODALIDADES, SERVICOS, Cobranca, Endpoint, Servico
from .client import RegistroChamada, RespostaSerpro, SerproClient
from .config import Credenciais, CredentialProvider, Procurador
from .preco import FAIXAS, custo
from .erros import (
    AindaProcessando,
    CertificadoVencido,
    ConfiguracaoIncompleta,
    ErroSerpro,
    FalhaAutenticacao,
    IntegraErro,
)

__all__ = [
    "SerproClient", "RespostaSerpro", "RegistroChamada", "Credenciais", "CredentialProvider", "Procurador",
    "SERVICOS", "MODALIDADES", "Servico", "Endpoint", "Cobranca", "custo", "FAIXAS",
    "IntegraErro", "ConfiguracaoIncompleta", "CertificadoVencido",
    "FalhaAutenticacao", "ErroSerpro", "AindaProcessando",
]
__version__ = "0.1.0"
