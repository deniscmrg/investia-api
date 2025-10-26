from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from typing import Optional, Literal
from datetime import datetime, timedelta, timezone
import time

import MetaTrader5 as mt5


app = FastAPI(title="MT5 API Demo v2", version="2.0")


# ===============================
# MODELOS DE ENTRADA
# ===============================

ExecucaoTipo = Literal["mercado", "limite", "stop"]


class Ordem(BaseModel):
    ticker: str
    tipo: Literal["compra", "venda"]
    quantidade: float = Field(gt=0, description="Volume a negociar")
    execucao: ExecucaoTipo = Field(
        default="mercado",
        description="Tipo de execução: mercado | limite | stop",
    )
    preco: Optional[float] = Field(
        default=None, description="Preço da ordem quando execucao != mercado"
    )
    sl: Optional[float] = Field(default=None, description="Stop loss")
    tp: Optional[float] = Field(default=None, description="Take profit")


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


def _normalize_price(symbol, price: float) -> float:
    """Normaliza preço para o múltiplo de point/digits do símbolo."""
    point = getattr(symbol, "point", 0.0) or 0.0
    if point <= 0:
        return float(price)
    steps = round(float(price) / float(point))
    return float(steps * point)


def _validate_volume(symbol, volume: float) -> tuple[bool, Optional[str]]:
    minv = getattr(symbol, "volume_min", None)
    maxv = getattr(symbol, "volume_max", None)
    step = getattr(symbol, "volume_step", None)
    if minv is None or maxv is None or step is None:
        return False, "Informações de volume indisponíveis para o símbolo"
    if volume < float(minv):
        return False, f"Quantidade mínima é {minv}"
    if volume > float(maxv):
        return False, f"Quantidade máxima é {maxv}"
    # checa múltiplo do step, considerando min como offset
    try:
        ratio = (float(volume) - float(minv)) / float(step)
        if abs(ratio - round(ratio)) > 1e-6:
            return (
                False,
                f"Quantidade deve respeitar o passo de {step} (múltiplos a partir de {minv})",
            )
    except Exception:
        return False, "Falha ao validar o passo de volume"
    return True, None


def _validate_limit_price(kind: str, price: float, tick) -> tuple[bool, Optional[str]]:
    """
    Regras usuais:
      - BUY_LIMIT: price <= ask
      - SELL_LIMIT: price >= bid
    """
    if tick is None:
        return False, "Sem tick disponível para validar preço"
    if kind == "BUY_LIMIT" and not (price <= float(tick.ask)):
        return False, f"Preço limite de compra deve ser <= {tick.ask}"
    if kind == "SELL_LIMIT" and not (price >= float(tick.bid)):
        return False, f"Preço limite de venda deve ser >= {tick.bid}"
    return True, None


def _validate_stop_price(kind: str, price: float, tick) -> tuple[bool, Optional[str]]:
    """
    Regras usuais:
      - BUY_STOP: price >= ask
      - SELL_STOP: price <= bid
    """
    if tick is None:
        return False, "Sem tick disponível para validar preço"
    if kind == "BUY_STOP" and not (price >= float(tick.ask)):
        return False, f"Preço stop de compra deve ser >= {tick.ask}"
    if kind == "SELL_STOP" and not (price <= float(tick.bid)):
        return False, f"Preço stop de venda deve ser <= {tick.bid}"
    return True, None


def _validate_stops_distance(symbol, order_type: int, price: float, sl: float | None, tp: float | None) -> tuple[bool, Optional[str]]:
    """Valida distância mínima de SL/TP conforme trade_stops_level do símbolo (em points)."""
    stops_level = getattr(symbol, "trade_stops_level", 0) or 0
    point = getattr(symbol, "point", 0.0) or 0.0
    if stops_level <= 0 or point <= 0:
        return True, None  # sem restrição explícita

    min_dist = float(stops_level) * float(point)

    # BUY: SL < price e TP > price; SELL: SL > price e TP < price
    from_types = {
        mt5.ORDER_TYPE_BUY: ("buy", 1),
        mt5.ORDER_TYPE_SELL: ("sell", -1),
        mt5.ORDER_TYPE_BUY_LIMIT: ("buy", 1),
        mt5.ORDER_TYPE_SELL_LIMIT: ("sell", -1),
        mt5.ORDER_TYPE_BUY_STOP: ("buy", 1),
        mt5.ORDER_TYPE_SELL_STOP: ("sell", -1),
    }
    side, direction = from_types.get(order_type, (None, None))
    if side is None:
        return True, None

    if sl is not None:
        dist = (price - sl) if side == "buy" else (sl - price)
        if dist <= 0:
            return False, "Stop loss deve ficar do lado oposto ao preço"
        if dist < min_dist:
            return False, f"Distância mínima do SL é {min_dist}"

    if tp is not None:
        dist = (tp - price) if side == "buy" else (price - tp)
        if dist <= 0:
            return False, "Take profit deve ficar do lado esperado do preço"
        if dist < min_dist:
            return False, f"Distância mínima do TP é {min_dist}"

    return True, None


def _build_order_request(o: Ordem, symbol, tick) -> dict:
    is_buy = o.tipo == "compra"

    if o.execucao == "mercado":
        order_type = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        price = float(tick.ask if is_buy else tick.bid)
        price = _normalize_price(symbol, price)
        filling = mt5.ORDER_FILLING_IOC
        action = mt5.TRADE_ACTION_DEAL
    elif o.execucao == "limite":
        order_type = mt5.ORDER_TYPE_BUY_LIMIT if is_buy else mt5.ORDER_TYPE_SELL_LIMIT
        if o.preco is None:
            raise HTTPException(400, "Campo 'preco' é obrigatório para ordens a limite")
        price = _normalize_price(symbol, float(o.preco))
        kind = "BUY_LIMIT" if is_buy else "SELL_LIMIT"
        ok, msg = _validate_limit_price(kind, price, tick)
        if not ok:
            raise HTTPException(400, msg)
        filling = mt5.ORDER_FILLING_RETURN
        action = mt5.TRADE_ACTION_PENDING
    elif o.execucao == "stop":
        order_type = mt5.ORDER_TYPE_BUY_STOP if is_buy else mt5.ORDER_TYPE_SELL_STOP
        if o.preco is None:
            raise HTTPException(400, "Campo 'preco' é obrigatório para ordens stop")
        price = _normalize_price(symbol, float(o.preco))
        kind = "BUY_STOP" if is_buy else "SELL_STOP"
        ok, msg = _validate_stop_price(kind, price, tick)
        if not ok:
            raise HTTPException(400, msg)
        filling = mt5.ORDER_FILLING_RETURN
        action = mt5.TRADE_ACTION_PENDING
    else:
        raise HTTPException(400, "Valor inválido para 'execucao'")

    # valida SL/TP se enviados
    ok, msg = _validate_stops_distance(symbol, order_type, price, o.sl, o.tp)
    if not ok:
        raise HTTPException(400, msg)

    req = {
        "action": action,
        "symbol": o.ticker,
        "volume": float(o.quantidade),
        "type": order_type,
        "price": price,
        "deviation": 20,
        "magic": 1001,
        "comment": f"API_MT5_v2_{o.execucao}",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling,
    }
    # inclui SL/TP somente se enviados
    if o.sl is not None:
        req["sl"] = float(o.sl)
    if o.tp is not None:
        req["tp"] = float(o.tp)
    return req


def _validar_ordem(o: Ordem) -> dict:
    ensure_mt5()
    ativar_simbolo(o.ticker)

    symbol = mt5.symbol_info(o.ticker)
    if symbol is None:
        raise HTTPException(404, f"Símbolo {o.ticker} não encontrado")

    ok, motivo = _validate_volume(symbol, float(o.quantidade))
    if not ok:
        return {
            "ok": False,
            "motivo": motivo,
            "regras": {
                "min": getattr(symbol, "volume_min", None),
                "max": getattr(symbol, "volume_max", None),
                "step": getattr(symbol, "volume_step", None),
            },
        }

    tick = mt5.symbol_info_tick(o.ticker)
    if tick is None:
        return {"ok": False, "motivo": "Sem tick disponível para o símbolo"}

    # validações de preço quando aplicável
    if o.execucao == "limite":
        if o.preco is None:
            return {"ok": False, "motivo": "Campo 'preco' é obrigatório para limite"}
        price = _normalize_price(symbol, float(o.preco))
        kind = "BUY_LIMIT" if o.tipo == "compra" else "SELL_LIMIT"
        ok, motivo = _validate_limit_price(kind, price, tick)
        if not ok:
            return {"ok": False, "motivo": motivo}
    elif o.execucao == "stop":
        if o.preco is None:
            return {"ok": False, "motivo": "Campo 'preco' é obrigatório para stop"}
        price = _normalize_price(symbol, float(o.preco))
        kind = "BUY_STOP" if o.tipo == "compra" else "SELL_STOP"
        ok, motivo = _validate_stop_price(kind, price, tick)
        if not ok:
            return {"ok": False, "motivo": motivo}

    # valida distância de SL/TP, quando enviados
    order_type = (
        mt5.ORDER_TYPE_BUY if o.execucao == "mercado" and o.tipo == "compra" else
        mt5.ORDER_TYPE_SELL if o.execucao == "mercado" and o.tipo == "venda" else
        mt5.ORDER_TYPE_BUY_LIMIT if o.execucao == "limite" and o.tipo == "compra" else
        mt5.ORDER_TYPE_SELL_LIMIT if o.execucao == "limite" and o.tipo == "venda" else
        mt5.ORDER_TYPE_BUY_STOP if o.execucao == "stop" and o.tipo == "compra" else
        mt5.ORDER_TYPE_SELL_STOP
    )
    ref_price = (
        float(tick.ask if o.tipo == "compra" else tick.bid)
        if o.execucao == "mercado"
        else float(o.preco)
    )
    ok, motivo = _validate_stops_distance(symbol, order_type, ref_price, o.sl, o.tp)
    if not ok:
        return {"ok": False, "motivo": motivo}

    return {
        "ok": True,
        "motivo": None,
        "regras": {
            "min": getattr(symbol, "volume_min", None),
            "max": getattr(symbol, "volume_max", None),
            "step": getattr(symbol, "volume_step", None),
        },
    }


# ===============================
# ENDPOINTS
# ===============================


@app.get("/status")
def status():
    """Retorna informações do terminal, conta e posições."""
    try:
        init = mt5.initialize() if not mt5.account_info() else True
        last_err = mt5.last_error()

        term = mt5.terminal_info()
        acc = mt5.account_info()

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
def historico_ordens(
    inicio: Optional[int] = None, fim: Optional[int] = None, symbol: Optional[str] = None
):
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
        ords = mt5.orders_get(symbol=symbol)
    else:
        ords = mt5.orders_get()

    if ords is None:
        erro = mt5.last_error()
        raise HTTPException(500, f"Falha ao obter ordens: {erro}")

    return [o._asdict() for o in ords]


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


@app.get("/validar-ordem")
def validar_ordem(
    ticker: str = Query(...),
    tipo: Literal["compra", "venda"] = Query(...),
    quantidade: float = Query(..., gt=0),
    execucao: ExecucaoTipo = Query("mercado"),
    preco: Optional[float] = Query(None),
    sl: Optional[float] = Query(None),
    tp: Optional[float] = Query(None),
):
    o = Ordem(ticker=ticker, tipo=tipo, quantidade=quantidade, execucao=execucao, preco=preco, sl=sl, tp=tp)
    return _validar_ordem(o)


@app.post("/ordem")
def ordem(o: Ordem):
    """Envia ordem de compra ou venda (mercado/limite/stop) com validações de volume e preço."""
    validation = _validar_ordem(o)
    if not validation.get("ok"):
        raise HTTPException(400, validation.get("motivo") or "Ordem inválida")

    ensure_mt5()
    ativar_simbolo(o.ticker)

    tick = mt5.symbol_info_tick(o.ticker)
    if tick is None:
        raise HTTPException(404, f"Sem dados para {o.ticker}")

    symbol = mt5.symbol_info(o.ticker)
    if symbol is None:
        raise HTTPException(404, f"Símbolo {o.ticker} não encontrado")

    req = _build_order_request(o, symbol, tick)

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
        "comment": "Fechamento_API_v2",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    r = mt5.order_send(req)
    return r._asdict() if r else {"erro": mt5.last_error()}
