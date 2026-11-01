# Worker do Integra Contador. Sem EXPOSE: este container não serve nada.
FROM python:3.12-slim

# lxml e signxml (assinatura XMLDSig do token de procurador) compilam contra
# libxml2/libxslt. Sem estes pacotes o build quebra só na instalação do lxml.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libxml2 libxslt1.1 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependências primeiro, numa camada própria: é o que mantém o pip em cache
# quando só o código muda. Lista explícita em vez de `pip install -e .` porque
# o modo editável exige o código presente, o que anularia o cache.
RUN pip install --no-cache-dir \
      "httpx>=0.27" "cryptography>=42" "signxml>=3.2" "lxml>=5" "psycopg[binary]>=3.1"

COPY pyproject.toml ./
COPY integra_serpro ./integra_serpro
COPY onstage ./onstage
RUN pip install --no-cache-dir --no-deps -e .

# Usuário sem privilégio: o processo carrega o e-CNPJ A1 em memória, e root não
# agrega nada a um processo que só faz saída de rede.
RUN useradd --create-home --uid 10001 worker
USER worker

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

# Sem HEALTHCHECK HTTP de propósito: a saúde é publicada em
# `integra_worker_pulso`. Health check por porta exigiria abrir uma porta — e foi
# exatamente isso que tornou o executável comprado inseguro.
CMD ["python", "-m", "onstage.main"]
