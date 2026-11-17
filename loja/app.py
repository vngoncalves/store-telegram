"""Inicialização e laço principal (long polling)."""
import os
import time

from . import config, handlers, payments, storage, telegram_api


def startup_checks():
    errors = config.config_errors()
    if errors:
        print("Configuração incompleta no arquivo .env:")
        for error in errors:
            print(" -", error)
        return False
    try:
        storage._read()
    except (RuntimeError, OSError) as exc:
        print("Não foi possível abrir o arquivo de dados:", exc)
        return False
    try:
        me = telegram_api.call("getMe")
    except Exception as exc:
        print("Não foi possível conectar ao Telegram. Confira o BOT_TOKEN e a internet.")
        print("Detalhe:", exc)
        return False
    print(f"Conectado como @{me.get('username')} (id {me.get('id')}).")
    try:
        # getUpdates não funciona enquanto houver webhook configurado.
        telegram_api.call("deleteWebhook", {"drop_pending_updates": "false"})
    except Exception as exc:
        print("Aviso: não foi possível remover um webhook existente:", exc)
    problems = payments.pix_config_problems()
    if problems:
        print("Aviso: recarga via Pix desativada —", "; ".join(problems) + ".")
    if os.name != "nt":
        print("Aviso: o cadastro e a exibição de licenças usam DPAPI e só funcionam no Windows.")
    return True


def main():
    if not startup_checks():
        raise SystemExit(1)
    print("Telegram Store iniciado. Dados em:", storage._PATH)
    offset = None
    last_payment_check = 0
    while True:
        try:
            updates = telegram_api.get_updates(offset=offset, timeout=config.POLLING_TIMEOUT)
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    handlers.process_update(update)
                except Exception as exc:
                    print(f"Erro ao processar a atualização {update['update_id']}:", exc)
        except KeyboardInterrupt:
            print("\nBot encerrado.")
            break
        except Exception as exc:
            print("Erro no loop:", exc)
            if "terminated by other getupdates request" in str(exc).lower():
                print("Outra cópia do bot está rodando com o mesmo token. Feche-a antes de continuar.")
            time.sleep(3)
        if time.monotonic() - last_payment_check >= config.PAYMENT_CHECK_INTERVAL:
            try:
                handlers.reconcile_pix_deposits()
            except KeyboardInterrupt:
                print("\nBot encerrado.")
                break
            except Exception as exc:
                print("Erro ao verificar cobranças Pix:", exc)
            last_payment_check = time.monotonic()
