from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional
import MetaTrader5 as mt5
import time

app = FastAPI(title="MT5 API Demo", version="1.0")

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
    """Conecta ao MT5 se ainda não estiver conectado"""
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
    """Ativa o símbolo se não estiver visível"""
    info = mt5.symbol_info(ticker)
    if info is None:
        raise HTTPException(404, f"Símbolo {ticker} não existe neste servidor")
    if not info.visible:
        if not mt5.symbol_select(ticker, True):
            raise HTTPException(500, f"Falha ao ativar símbolo {ticker}")


# ===============================
# ENDPOINTS
# ===============================

@app.get("/status")
def status():
    """Retorna informações do terminal e da conta"""
    try:
        init = mt5.initialize()
        last_err = mt5.last_error()

        term = mt5.terminal_info()
        acc = mt5.account_info()

        return {
            "mt5_initialize": init,
            "last_error": last_err,
            "terminal": {
                "connected": term.connected if term else None,
                "trade_allowed": term.trade_allowed if term else None,
                "server": term.server if term else None,
                "ping": term.ping_last if term else None,
            },
            "conta": {
                "login": acc.login if acc else None,
                "name": acc.name if acc else None,
                "balance": acc.balance if acc else None,
                "equity": acc.equity if acc else None,
                "currency": acc.currency if acc else None,
            },
        }
    except Exception as e:
        return {"erro": str(e)}


@app.get("/cotacao/{ticker}")
def cotacao(ticker: str):
    """Retorna última cotação, mínima e máxima do dia"""
    ensure_mt5()
    ativar_simbolo(ticker)

    tick = mt5.symbol_info_tick(ticker)
    if tick is None:
        raise HTTPException(404, f"Sem tick para {ticker}")

    # Candle diário (último dia)
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


@app.post("/ordem")
def ordem(o: Ordem):
    """Envia ordem de compra ou venda"""
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
    """Ajusta Stop Gain / Stop Loss de uma posição"""
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
    """Fecha posição"""
    ensure_mt5()
    pos = mt5.positions_get(ticket=ticket)
    if not pos:
        raise HTTPException(404, "Ticket não encontrado")
    p = pos[0]
    t = mt5.symbol_info_tick(p.symbol)
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

