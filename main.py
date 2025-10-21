from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import MetaTrader5 as mt5
import time
from datetime import datetime, timedelta, timezone

app = FastAPI(title="MT5 API Demo", version="1.1")

# ===============================
# MODELOS DE ENTRADA
# ===============================

class Ordem(BaseModel):
    ticker: str
    tipo: str  # "compra" ou "venda"
    quantidade: float
    preco: Optional[float] = None
    sl: Optional[float] = None
    tp: Optional[float] = None


class AjusteStop(BaseModel):
    ticket: int
    stop_gain: Optional[float] = None
    stop_loss: Optional[float] = None


# ===============================
# FUNÇÕES AUXILIARES
# ===============================

def ensure_mt5():
    """Conecta ao MT5 se ainda não estiver conectado."""
    if mt5.account_info() is not None:
        return True
    if not mt5.initialize():
        raise HTTPException(500, f"Erro ao inicializar MT5: {mt5.last_error()}")
    for _ in range(10):
        if mt5.account_info() is not None:
            return True
        time.sleep(0.3)
    raise HTTPException(500, "MT5 não conectado")


def ativar_simbolo(ticker: str):
    """Ativa o símbolo se não estiver visível."""
    info = mt5.symbol_info(ticker)
    if info is None:
        raise HTTPException(404, f"Símbolo {ticker} não existe neste servidor.")
    if not info.visible:
        if not mt5.symbol_select(ticker, True):
            raise HTTPException(500, f"Falha ao ativar símbolo {ticker}.")


# ===============================
# ENDPOINTS
# ===============================

@app.get("/status")
def status():
    """Retorna informações do terminal, conta e posições."""
    try:
        # Evita reinicializar se já estiver ativo
        init = mt5.initialize() if not mt5.account_info() else True
        last_err = mt5.last_error()

        term = mt5.terminal_info()
        acc = mt5.account_info()

        # Posições e ordens abertas (resumo opcional)
        posicoes = mt5.positions_get()
        ordens = mt5.orders_get()

        return {
            "mt5_initialize": init,
            "last_error": last_err,
            "terminal": {
                "connected": getattr(term, "connected", None),
                "trade_allowed": getattr(term, "trade_allowed", None),
                "ping": getattr(term, "ping_last", None),
            },
            "conta": {
                "login": getattr(acc, "login", None),
                "name": getattr(acc, "name", None),
                "balance": getattr(acc, "balance", None),
                "equity": getattr(acc, "equity", None),
                "currency": getattr(acc, "currency", None),
                "server": getattr(acc, "server", None),
            },
            "resumo": {
                "posicoes_abertas": len(posicoes) if posicoes else 0,
                "ordens_pendentes": len(ordens) if ordens else 0,
            },
        }
    except Exception as e:
        return {"erro": str(e)}


@app.get("/cotacao/{ticker}")
def cotacao(ticker: str):
    """Retorna última cotação, mínima e máxima do dia."""
    ensure_mt5()
    ativar_simbolo(ticker)

    tick = mt5.symbol_info_tick(ticker)
    if tick is None:
        raise HTTPException(404, f"Sem tick para {ticker}")

    d1 = mt5.copy_rates_from_pos(ticker, mt5.TIMEFRAME_D1, 0, 1)
    bar = d1[0] if d1 is not None and len(d1) else None

    return {
        "ticker": ticker,
        "bid": tick.bid,
        "ask": tick.ask,
        "last": tick.last,
        "min": bar["low"] if bar is not None else None,
        "max": bar["high"] if bar is not None else None,
        "time": tick.time,
    }


@app.get("/posicoes")
def listar_posicoes():
    """Retorna todas as posições abertas com informações de stop gain/stop loss."""
    ensure_mt5()

    posicoes = mt5.positions_get()
    if posicoes is None:
        erro = mt5.last_error()
        raise HTTPException(500, f"Falha ao obter posições: {erro}")

    retorno = []
    for p in posicoes:
        retorno.append(
            {
                "ticket": p.ticket,
                "symbol": p.symbol,
                "type": p.type,
                "volume": p.volume,
                "price_open": p.price_open,
                "price_current": p.price_current,
                "sl": p.sl,
                "tp": p.tp,
                "profit": p.profit,
                "swap": p.swap,
                "commission": p.commission,
                "time": p.time,
                "time_msc": p.time_msc,
                "magic": p.magic,
                "comment": p.comment,
                "identifier": p.identifier,
            }
        )

    return retorno


@app.get("/historico")
def historico(inicio: Optional[int] = None, fim: Optional[int] = None):
    """
    Retorna histórico de operações (deals) no intervalo desejado.

    Parâmetros aceitam epoch segundos; sem parâmetros busca últimos 30 dias.
    """
    ensure_mt5()

    agora = datetime.now(timezone.utc)
    fim_dt = datetime.fromtimestamp(fim, tz=timezone.utc) if fim else agora
    inicio_dt = (
        datetime.fromtimestamp(inicio, tz=timezone.utc)
        if inicio
        else fim_dt - timedelta(days=30)
    )

    if inicio_dt >= fim_dt:
        raise HTTPException(400, "Parâmetros inválidos: inicio deve ser anterior a fim.")

    deals = mt5.history_deals_get(inicio_dt, fim_dt)
    if deals is None:
        erro = mt5.last_error()
        raise HTTPException(500, f"Falha ao obter histórico: {erro}")

    return [d._asdict() for d in deals]


@app.get("/historico-ordens")
def historico_ordens(inicio: Optional[int] = None, fim: Optional[int] = None, symbol: Optional[str] = None):
    """
    Retorna histórico de ordens (inclui enviadas, modificadas e canceladas).

    Parâmetros aceitam epoch segundos; sem parâmetros busca últimos 30 dias.
    """
    ensure_mt5()

    agora = datetime.now(timezone.utc)
    fim_dt = datetime.fromtimestamp(fim, tz=timezone.utc) if fim else agora
    inicio_dt = (
        datetime.fromtimestamp(inicio, tz=timezone.utc)
        if inicio
        else fim_dt - timedelta(days=30)
    )

    if inicio_dt >= fim_dt:
        raise HTTPException(400, "Parâmetros inválidos: inicio deve ser anterior a fim.")

    if symbol:
        ativar_simbolo(symbol)
        orders = mt5.history_orders_get(inicio_dt, fim_dt, group=symbol)
    else:
        orders = mt5.history_orders_get(inicio_dt, fim_dt)

    if orders is None:
        erro = mt5.last_error()
        raise HTTPException(500, f"Falha ao obter histórico de ordens: {erro}")

    return [o._asdict() for o in orders]


@app.get("/ordens")
def ordens(symbol: Optional[str] = None):
    """Retorna ordens pendentes; aceita filtro opcional por símbolo."""
    ensure_mt5()

    if symbol:
        ativar_simbolo(symbol)
        ordens = mt5.orders_get(symbol=symbol)
    else:
        ordens = mt5.orders_get()

    if ordens is None:
        erro = mt5.last_error()
        raise HTTPException(500, f"Falha ao obter ordens: {erro}")

    return [o._asdict() for o in ordens]

@app.get("/conta")
def conta():
    """Retorna informações detalhadas da conta."""
    ensure_mt5()

    info = mt5.account_info()
    if info is None:
        erro = mt5.last_error()
        raise HTTPException(500, f"Falha ao obter informações da conta: {erro}")

    return info._asdict()


@app.get("/simbolo/{ticker}")
def simbolo(ticker: str):
    """Retorna metadados do símbolo informado."""
    ensure_mt5()
    ativar_simbolo(ticker)

    info = mt5.symbol_info(ticker)
    if info is None:
        raise HTTPException(404, f"Símbolo {ticker} não encontrado.")

    return info._asdict()


@app.post("/ordem")
def ordem(o: Ordem):
    """Envia ordem de compra ou venda."""
    ensure_mt5()
    ativar_simbolo(o.ticker)

    if o.tipo not in ["compra", "venda"]:
        raise HTTPException(400, "Tipo deve ser 'compra' ou 'venda'")

    tick = mt5.symbol_info_tick(o.ticker)
    if tick is None:
        raise HTTPException(404, f"Sem dados para {o.ticker}")

    tipo_ordem = mt5.ORDER_TYPE_BUY if o.tipo == "compra" else mt5.ORDER_TYPE_SELL
    preco = o.preco or (tick.ask if o.tipo == "compra" else tick.bid)

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": o.ticker,
        "volume": o.quantidade,
        "type": tipo_ordem,
        "price": preco,
        "deviation": 20,
        "magic": 1001,
        "comment": "API_MT5",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
        "sl": o.sl,
        "tp": o.tp,
    }

    result = mt5.order_send(req)
    if not result:
        raise HTTPException(500, f"Erro ao enviar ordem: {mt5.last_error()}")
    return result._asdict()


@app.post("/ajustar-stop")
def ajustar_stop(a: AjusteStop):
    """Ajusta Stop Gain / Stop Loss de uma posição."""
    ensure_mt5()
    pos = mt5.positions_get(ticket=a.ticket)
    if not pos:
        raise HTTPException(404, "Posição não encontrada")

    p = pos[0]
    req = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": p.symbol,
        "position": a.ticket,
        "sl": a.stop_loss if a.stop_loss is not None else p.sl,
        "tp": a.stop_gain if a.stop_gain is not None else p.tp,
    }
    r = mt5.order_send(req)
    return r._asdict() if r else {"erro": mt5.last_error()}


@app.post("/fechar/{ticket}")
def fechar(ticket: int):
    """Fecha uma posição pelo ticket informado."""
    ensure_mt5()
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        raise HTTPException(404, "Ticket não encontrado")

    p = pos[0]
    t = mt5.symbol_info_tick(p.symbol)
    if not t:
        raise HTTPException(404, f"Sem dados de tick para {p.symbol}")

    tipo_contra = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    preco = t.bid if p.type == mt5.ORDER_TYPE_BUY else t.ask

    req = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": p.symbol,
        "volume": p.volume,
        "type": tipo_contra,
        "position": ticket,
        "price": preco,
        "deviation": 20,
        "magic": 1001,
        "comment": "Fechamento_API",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    r = mt5.order_send(req)
    return r._asdict() if r else {"erro": mt5.last_error()}
