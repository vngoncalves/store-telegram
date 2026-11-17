"""Telegram Store — ponto de entrada.

Execute na pasta do projeto:  python store_bot.py
O código fica no pacote loja/; a configuração no arquivo .env.
"""
from loja.app import main


if __name__ == "__main__":
    main()
