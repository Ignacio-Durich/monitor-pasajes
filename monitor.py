#!/usr/bin/env python3
"""Monitor de pasajes Buenos Aires <-> Colonia (viaje a Punta del Este, dic 2026).

Uso:
  python3 monitor.py            revisa precios y códigos, avisa solo si hay novedad
  python3 monitor.py --resumen  muestra los mínimos guardados y el historial
  python3 monitor.py --test     chequeo rápido de la lógica de avisos
  python3 monitor.py --probar   manda un aviso de prueba (para verificar Telegram y mail)
  python3 monitor.py --diario   revisa y manda el resumen del día, haya o no novedades
  python3 monitor.py --ejemplo  revisa de verdad y manda un aviso de baja simulada, marcado como ejemplo
  python3 monitor.py --chatid   muestra tu chat_id de Telegram (después de escribirle al bot)

El viaje (tramos, fechas, membresía) y las claves de los avisos van en config.json (ver config.ejemplo.json),
que no se sube al repositorio. Avisos: Telegram y/o mail;
si no hay ninguno configurado, imprime el aviso en pantalla.
"""
import html, json, os, re, shutil, smtplib, sqlite3, subprocess, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

AQUI = Path(__file__).parent
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"
CE_API = "https://book.coloniaexpress.com/api"
CE_WEB = "https://www.coloniaexpress.com"

# --- Qué se vigila -----------------------------------------------------------
# El viaje no está en el código: se lee de "viaje" en config.json (privado) o, si falta, del ejemplo.
# Cada tramo: id, nombre, corto (para frases), ruta (BUECYR o CYRBUE), pax, auto, fechas {día: hora mínima}
# y flex (además del más barato, seguir la tarifa que permite cambiar la fecha).
def _cfg(nombre="config.json"):
    f = AQUI / nombre
    return json.loads(f.read_text()) if f.exists() else {}


def cargar_viaje(v):
    global TRAMOS, FECHAS_VIAJE, SOCIO, NOMBRES, SERIES
    TRAMOS, FECHAS_VIAJE, SOCIO = v["tramos"], tuple(v.get("fechas_codigos", ())), v.get("membresia")
    NOMBRES, SERIES = {}, {}   # clave -> (título, sujeto de frase) / (tramo, filtro de tarifa para el gráfico)
    for t in TRAMOS:
        if t.get("flex"):
            NOMBRES[f"{t['id']}|con cambio de fecha"] = (f"{t['nombre']}, con cambio de fecha", f"{t['corto']} con cambio")
            NOMBRES[f"{t['id']}|mas barato"] = (f"{t['nombre']}, sin cambio", f"{t['corto']} sin cambio")
            SERIES[f"{t['id']}|con cambio de fecha"] = (t["id"], "%Flex%")
        else:
            NOMBRES[f"{t['id']}|mas barato"] = (t["nombre"], t["corto"])
            SERIES[f"{t['id']}|mas barato"] = (t["id"], "%")
    if SOCIO:
        NOMBRES["socio|mas barato"] = (f"Total con {SOCIO['nombre']}", f"El total con {SOCIO['nombre']}")
        SERIES["socio|mas barato"] = ("socio", "%")


cargar_viaje(_cfg().get("viaje") or _cfg("config.ejemplo.json")["viaje"])
BQB_HOME = "https://www.buquebus.com/api/paginas?where[slug][equals]=home&where[unidadDeNegocio][equals]=ar&locale=es"
BQB_BASES = "https://www.buquebus.com/api/paginas?where[slug][equals]=bases-y-condiciones&where[unidadDeNegocio][equals]=ar&locale=es"
# Blog que cubre las promos de los dos ferris; se avisa cuando hay una nota nueva.
BLOGS = ["https://www.sirchandler.com.ar/tag/colonia-express/feed/",
         "https://www.sirchandler.com.ar/tag/buquebus/feed/"]


def http(url, data=None, headers=None):
    h = {"User-Agent": UA, "Accept-Language": "es", **(headers or {})}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h)
    time.sleep(0.4)  # no martillar los sitios
    with urllib.request.urlopen(req, timeout=40) as r:
        return r.read().decode("utf-8", "replace")


def texto_plano(pagina):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", pagina, flags=re.S)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", t))).strip()


# --- Colonia Express ---------------------------------------------------------
def ce_salidas(ruta, fecha, pax, auto, cupon_firmado=None):
    pedido = {"routes": [{"route": ruta, "departureDate": fecha}],
              "passengerTypes": ["A"] * pax,
              "vehicles": [{"vehicleType": "Small"}] if auto else [],
              "searchMode": "Regular"}
    if cupon_firmado:
        pedido["couponNumber"] = cupon_firmado
    return json.loads(http(f"{CE_API}/booking/departures/search", pedido, {"X-Country": "ARG"}))


def ce_codigos():
    """Códigos publicados en /beneficios/: {codigo: {titulo, url, aplica_a_mis_fechas}}."""
    indice = http(f"{CE_WEB}/es/ar/beneficios/")
    out = {}
    for ruta in sorted(set(re.findall(r'href="(/es/ar/beneficios/[^"]+)"', indice))):
        try:
            t = texto_plano(http(CE_WEB + ruta))
        except urllib.error.URLError:
            continue
        m = re.search(r"C[oó]digo(?: de descuento)?\s*:\s*([A-Z0-9_-]{3,})", t)
        if not m:
            continue  # beneficios por membresía (Clarín, La Nación, etc.), sin código público
        titulo = t.split("|")[0].strip()
        out[m.group(1)] = dict(titulo=titulo, url=CE_WEB + ruta,
                               aplica=[f for f in FECHAS_VIAJE if f in t])
    return out


def ce_firmar(codigo):
    try:
        return json.loads(http(f"{CE_API}/coupon/{urllib.parse.quote(codigo)}", headers={"X-Country": "ARG"})).get("signedCoupon")
    except (urllib.error.URLError, ValueError):
        return None


def opciones_ce(tramo, cupon=None, firmado=None):
    """Lista plana de opciones comprables para un tramo."""
    out = []
    for fecha, desde in tramo["fechas"].items():
        for s in ce_salidas(tramo["ruta"], fecha, tramo["pax"], tramo["auto"], firmado):
            if s["departureTime"] < desde:
                continue
            for c in s["categories"]:
                if not c["isBookable"] or c["price"] is None or c["ticketType"] == "CRU":
                    continue  # CRU = ida y vuelta en el día, no sirve
                out.append(dict(empresa="Colonia Express", tramo=tramo["id"], fecha=fecha,
                                hora=s["departureTime"], tarifa=c["name"], tipo=c["ticketType"],
                                precio=c["price"], moneda=s["currency"], cupon=cupon,
                                flex=c["ticketType"] == "TTA", id_ce=c["identifier"],
                                link=("https://book.coloniaexpress.com/es/ar/booking?from=%s&to=%s&dateRange.start=%s"
                                      "&tripType=OneWay&adults=%d&children=0&infants=0%s"
                                      % (tramo["ruta"][:3], tramo["ruta"][3:], fecha, tramo["pax"],
                                         "&vehicle=Small" if tramo["auto"] else "")),
                                quedan_pocos=c.get("fewLeft", False)))
    return out


# Beneficio de socio en Colonia Express (p. ej. Clarín 365 o Club La Nación: 25% en pasajes de ida y vuelta).
# No cubre bodega ni tasas, no vale para pasajes abiertos y un cambio de fecha hace perder el descuento.


def _neto_pasajes(o):
    """Tarifa de pasajeros sin impuestos de una opción de Colonia Express (sobre eso se aplica el descuento)."""
    r = json.loads(http(f"{CE_API}/booking/calculate", {"identifiers": [o["id_ce"]], "type": "Departure"}, {"X-Country": "ARG"}))
    return sum(x["price"] - x["taxes"] for x in r["sections"][0]["rows"] if x["type"] == "Passenger")


def con_membresia(resto):
    """Total estimado con el beneficio de socio (config "membresia"): los que vuelven en el tramo "vuelta"
    compran ida y vuelta por Colonia Express con descuento; el resto de la ida va suelto en el mismo barco.
    resto = lo que suman los demás tramos. Devuelve (total, ahorro, ida, vuelta)."""
    ida = next(t for t in TRAMOS if t["id"] == SOCIO["ida"])
    vuelta = next(t for t in TRAMOS if t["id"] == SOCIO["vuelta"])
    barato = lambda ops: min(ops, key=lambda o: o["precio"])
    a_ida, a_vuelta = barato(opciones_ce(dict(ida, pax=vuelta["pax"]))), barato(opciones_ce(vuelta))
    sueltos = ida["pax"] - vuelta["pax"]
    b = 0
    if sueltos > 0:
        ops = opciones_ce(dict(ida, pax=sueltos, auto=False))
        b = barato([o for o in ops if (o["fecha"], o["hora"]) == (a_ida["fecha"], a_ida["hora"])] or ops)["precio"]
    ahorro = SOCIO["descuento"] * (_neto_pasajes(a_ida) + _neto_pasajes(a_vuelta))
    return a_ida["precio"] + a_vuelta["precio"] - ahorro + b + resto, ahorro, a_ida, a_vuelta


# --- Buquebus -----------------------------------------------------------------
BQB_API = "https://www.buquebus.com/api"
BQB_PUERTOS = {"BUE": "BUE", "CYR": "COL"}  # mismos tramos que Colonia Express, con los códigos de Buquebus
BQB_CLASES = {"ESEAT": "Economy", "TSEAT": "Turista"}  # las dos clases más baratas
_bqb_token = None


def bqb_salidas(origen, destino, fecha, pax, auto, clase):
    """Consulta de disponibilidad tal como la arma el buscador de buquebus.com."""
    global _bqb_token
    if _bqb_token is None:  # sesión anónima que el sitio le da a cualquier visitante
        _bqb_token = json.loads(http(f"{BQB_API}/edea_login", {}))["token"]
    f = fecha[2:4] + fecha[5:7] + fecha[8:10]  # AAMMDD
    tramo1 = {"m_LEGJ_LegOrSectorOfJourney": "1"}
    adultos = [{"m_U257_PassengerTypeCode": "ADL", "m_U258_NumberOfPassengers": pax}]
    tarifas = ["ECONOMICA"] if clase == "ESEAT" else ["PROGRAMADA", "FLEXIBLE"]
    pedido = {
        "m_AGT_AgencyIdentity": {
            "m_AGAC_AgentsAccountNumber": {"m_AGAC_AgentAccountNumber": "7250"},
            "c_CURR_CurrencyForTransaction": {"m_6345_CurrencyCoded": "ARS", "c_U428_DecimalPrecision": "2"},
            "c_CMPNY_Company": {"m_C045_TradingUnitName": "PAX", "m_C046_CompanyName": "BUQUEBUS",
                                "m_C047_GeographycalLocationName": "South America", "m_C048_DivisionName": "BQB"},
            "c_SCHN_SalesChannel": {"m_C056_SalesChannel": "WEB"}},
        "m_RDQ_RouteDateTimeRequest": [{
            "index": "0", "m_LEGJ_LegOrSectorOfJourney": tramo1,
            "m_ROUT_TravelRoute": {
                "m_U271_ServiceAgreementDeparturePort": origen, "m_U272_ServiceAgreementDestinationPort": destino,
                "c_C276_SailingCode": "", "c_C040_ServiceAgreementCode": "", "c_C041_ServiceAgreementValidationCode": "",
                "c_C144_ServiceAgreementGroupType": "", "m_C001_ServiceAgreementOfferCode": "",
                "c_C039_ServiceAgreementOfferValidationCode": "", "c_C257_applyReturnFare": False,
                "c_399_SearchVesselTransfer": "false", "c_400_vesselTransferTime": 0},
            "m_DPDT_DepartureDateTime": {"m_U247_StandardDepartureDate": f, "m_U248_StandardDepartureTime": "0000"},
            "c_DPDT_DepartureDateTimeTo": {"m_U247_StandardDepartureDate": f, "m_U248_StandardDepartureTime": "2359"}}],
        "c_PAQ_PassengerDetailRequest": [{"index": "0", "m_LEGJ_LegOrSectorOfJourney": tramo1,
                                          "m_PAXS_PassengerSet": [{"passengerIndex": "0", **adultos[0]}]}],
        "c_ACQ_AccomodationDetailsRequest": [{
            "m_LEGJ_LegOrSectorOfJourney": tramo1,
            "m_ACPL_AccomodationPlaces": {"m_U228_QuantityOfUnits": pax, "m_U229_ModeOfOccupancy": "C"},
            "m_ACDT_AccomodationDetails": {"m_U220_ServiceAgreementDefinedAccomodationCode": clase, "i_U221_AccomodationType": "CHAIR"},
            "m_PAXT_PassengerType": adultos}],
        "c_VEQ_VehicleRequest": [{"index": 0, "m_LEGJ_LegOrSectorOfJourney": {"m_LEGJ_LegOrSectorOfJourney": 1},
                                  "m_VEHL_VehicleDetails": {"m_U294_VehicleTypeCode": "CAR", "c_U298_VehicleRegistration": "",
                                                            "c_U299_VehicleMake": "", "c_U160_VehicleModel": ""}}] if auto else [],
        "c_MTT_MultipleTariffTypeRequest": [{"m_LEGJ_LegOrSectorOfJourney": tramo1, "m_TARF_TariffCodeTypeDescription":
                                             [{"c_U282_TariffType": t, "c_C113_PriceDetailRequested": "true"} for t in tarifas]}],
    }
    return json.loads(http(f"{BQB_API}/priceAvailability", {"token": _bqb_token, "request": pedido})).get("sailingprice") or []


def _bruto(x):
    """Suma de tarifa + tasas dentro de x, en centavos. Las líneas DISCOUNT son informativas:
    la tarifa (PRODUCT) ya viene con el descuento aplicado, así que no se suman."""
    if isinstance(x, dict):
        propio = sum(int(d.get("m_C088_UnitGrossAmount", 0)) for d in x.get("c_PRIC_PriceDetails", [])
                     if d.get("c_C084_Type") in ("PRODUCT", "SURCHARGE"))
        return propio * int(x.get("m_U258_NumberOfPassengers", 1) or 1) + sum(_bruto(v) for v in x.values())
    if isinstance(x, list):
        return sum(_bruto(v) for v in x)
    return 0


def opciones_bqb(tramo):
    out = []
    origen, destino = BQB_PUERTOS[tramo["ruta"][:3]], BQB_PUERTOS[tramo["ruta"][3:]]
    for fecha, desde in tramo["fechas"].items():
        for clase, nombre in BQB_CLASES.items():
            for s in bqb_salidas(origen, destino, fecha, tramo["pax"], tramo["auto"], clase):
                hhmm = s["c_RDR_RouteDateTimeResponse"]["m_DPDT_DepartureDateAndTime"]["m_U248_StandardDepartureTime"]
                hora = f"{hhmm[:2]}:{hhmm[2:]}"
                if hora < desde:
                    continue
                # El "total" que devuelve Buquebus es solo la tarifa: las tasas (puerto, migraciones)
                # vienen aparte en productPrice, así que el precio real es la suma de componentes.
                for pp in s.get("productPrice", []):
                    tipo = pp["c_U282_TariffType"]
                    total = _bruto(pp)
                    if not total:
                        continue  # sin lugar en esa clase y tarifa
                    out.append(dict(empresa="Buquebus", tramo=tramo["id"], fecha=fecha, hora=hora,
                                    tarifa=f"{nombre} {tipo.capitalize()}", tipo=tipo, precio=total / 100,
                                    moneda="ARS", cupon=None, flex=tipo == "FLEXIBLE", quedan_pocos=False,
                                    link="https://www.buquebus.com/ar?origin=%s&destination=%s&departureDate=%s-%s-%s&adults=%d&cars=%d"
                                         % (origen, destino, fecha[8:], fecha[5:7], fecha[:4], tramo["pax"], int(tramo["auto"]))))
    return out


# --- Promos de Buquebus y blog -----------------------------------------------
def _textos(x):
    if isinstance(x, dict):
        if isinstance(x.get("text"), str) and x["text"].strip():
            yield x["text"].strip()
        for v in x.values():
            yield from _textos(v)
    elif isinstance(x, list):
        for v in x:
            yield from _textos(v)


def _banners(x):
    if isinstance(x, dict):
        if x.get("blockType") == "bannerBlock":
            yield " | ".join(dict.fromkeys(_textos(x)))
        for v in x.values():
            yield from _banners(v)
    elif isinstance(x, list):
        for v in x:
            yield from _banners(v)


def _fin_de_compra(txt):
    """Fecha en que termina una ventana 'compras del X al Y' (la segunda fecha completa, o la única), o None."""
    f = re.findall(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", txt)
    if not f:
        return None
    d, m, a = (int(x) for x in f[min(1, len(f) - 1)])
    try:
        return date(a + 2000 if a < 100 else a, m, d)
    except ValueError:
        return None


def cupones_de(tokens):
    """Busca 'cupón: CODIGO' en una lista de textos y devuelve {CODIGO: {titulo, compra, vence}}."""
    out = {}
    for i, tok in enumerate(tokens):
        tok = tok.strip()
        # "cupón: XXXX", "ingresando el código XXXX", o "cupón" al final con el código en el texto siguiente
        m = (re.search(r"(?i)\bcup[oó]n\s*:\s*([A-Za-z0-9]{4,})", tok)
             or re.search(r"(?i)\bingresando el (?:cup[oó]n|c[oó]digo)\s+([A-Za-z0-9]{4,})", tok)
             or re.search(r"(?i)\bcup[oó]n\s*:?\s*()$", tok))
        if not m:
            continue
        cod = m.group(1) or (tokens[i + 1].strip() if i + 1 < len(tokens) else "")
        if not re.fullmatch(r"[A-Za-z0-9]{4,}", cod):
            continue
        titulo = re.sub(r"(?i)\s*(aplicando\s+)?$", "", tok[:m.start()]).strip(" |-\n") or (tokens[i - 1].strip() if i else "")
        # Ventana de compra: primero en lo que sigue al código (sin pasar a la promo siguiente),
        # después en los textos cortos de antes (así vienen los banners del inicio).
        compra, vence = "", None
        legal = "ingresando" in m.group(0).lower()   # texto legal: su ventana está al principio del mismo texto
        despues = [tok] if legal else [tok[m.end():]]
        for t in ([] if legal else tokens[i + 1:i + 4]):
            if re.search(r"(?i)cup[oó]n\s*:", t):
                break
            despues.append(t)
        if legal:
            titulo = tokens[i - 1].strip() if i and len(tokens[i - 1]) < 120 else ""
        for t in despues + ([] if legal else [t for t in tokens[max(0, i - 2):i] if len(t) < 70]):
            c = re.search(r"(?i)(para compras|compras a realizar|promoci[oó]n v[aá]lida)", t)
            vence = _fin_de_compra(re.sub(r"\s+", " ", t[c.start():c.start() + 130])) if c else None
            if vence:
                compra = f"se puede comprar hasta el {vence.day}/{vence.month}/{vence.year}"
                break
        previo = out.get(cod.upper())
        if not previo or (vence and (not previo["vence"] or vence > previo["vence"])):
            titulo = re.sub(r"\s+", " ", re.sub(r"(?i)\|?\s*cup[oó]n\s*:.*", "", titulo)).strip(" |") or (previo or {}).get("titulo", "")
            out[cod.upper()] = dict(titulo=titulo[:70], compra=compra, vence=vence)
    return out


def bqb_cupones():
    """Cupones de Buquebus Argentina: bases y condiciones (lista oficial) + banners del inicio."""
    out = {}
    for url in (BQB_BASES, BQB_HOME):
        for doc in json.loads(http(url)).get("docs", []):
            for cod, info in cupones_de(list(_textos(doc))).items():
                if cod not in out or (info["vence"] and (not out[cod]["vence"] or info["vence"] > out[cod]["vence"])):
                    out[cod] = info
    return out


def notas_blog():
    out = []
    for url in BLOGS:
        feed = http(url)
        out += [html.unescape(t) for t in re.findall(r"<item>.*?<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", feed, flags=re.S)]
    return sorted(set(out))


# --- Lógica de avisos --------------------------------------------------------
def candidatos(tramo, opciones):
    """Qué mínimos se siguen por tramo: el más barato y, si aplica, el flexible."""
    c = {}
    if opciones:
        c[f"{tramo['id']}|mas barato"] = min(opciones, key=lambda o: o["precio"])
    flex = [o for o in opciones if o["flex"]]
    if tramo.get("flex") and flex:
        c[f"{tramo['id']}|con cambio de fecha"] = min(flex, key=lambda o: o["precio"])
    return c


def bajas(mejores, actuales):
    """Devuelve [(clave, opcion, minimo_anterior)] cuando el precio baja del mínimo guardado.
    La primera vez que se ve una clave no es una baja: es la línea de base."""
    return [(k, o, mejores[k]) for k, o in actuales.items() if k in mejores and o["precio"] < mejores[k]]


def plata(n):
    return f"${n:,.0f}".replace(",", ".")


def linea(o):
    cup = f" con código {o['cupon']}" if o.get("cupon") else ""
    pocos = " (quedan pocos)" if o.get("quedan_pocos") else ""
    return f"{o['empresa']} {o['fecha'][8:]}/{o['fecha'][5:7]} {o['hora']} {o['tarifa']}: {plata(o['precio'])} {o['moneda']}{cup}{pocos}"


# --- Avisos ------------------------------------------------------------------
DIAS = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
ICONOS = {"baja": "🔻", "suba": "🔺", "pocos": "⚠️", "codigo": "🏷️", "nota": "📰"}
NO_SIRVEN = r"(?i)day ?tour|frecuenta|paquete|abono|cuponera|daybue|daymvd|fullday"  # cupones que no son de pasajes


def elegido(actuales, t):
    """La opción de un tramo que entra en el total: la que permite cambio si el tramo es flexible."""
    if t.get("flex"):
        return actuales.get(f"{t['id']}|con cambio de fecha") or actuales.get(f"{t['id']}|mas barato")
    return actuales.get(f"{t['id']}|mas barato")


def cuando(o):
    d = date.fromisoformat(o["fecha"])
    return f"{DIAS[d.weekday()]} {d.day}/{d.month} {o['hora']}"




def series(db):
    """El más barato de cada día por tramo, del historial: {clave: [(día, precio)]}."""
    return {k: db.execute("SELECT substr(ts,1,10) d, MIN(precio) FROM precios WHERE tramo=? AND tarifa LIKE ? GROUP BY d ORDER BY d", f).fetchall()
            for k, f in SERIES.items()}


def grafico(series, ahora=None):
    """Evolución del precio, un punto por día. Devuelve (html para el mail, texto para Telegram).
    ahora = {clave: precio de esta revisión}; sin eso se usa el más barato del último día.
    En Telegram no hay gráfico (las barras de texto no se leen): va cuánto está cada tramo sobre su mínimo.
    El mail no admite imágenes generadas sin dependencias, así que el gráfico es una tabla con un punto por columna."""
    dia = lambda d: f"{int(d[8:])}/{int(d[5:7])}"
    h, g = "", []
    for k, puntos in series.items():
        if not puntos:
            continue
        ps = [p for _, p in puntos]
        hoy = (ahora or {}).get(k, ps[-1])
        lo, hi = min(ps), max(ps)
        alto = lambda p: (p - lo) / (hi - lo) if hi > lo else 0.5   # 0 = el más barato, 1 = el más caro
        dia_lo = dia(next(d for d, p in puntos if p == lo))
        celdas = "".join(
            f"<td style='vertical-align:top;padding:4px 1px' title='{dia(d)}: {plata(p)}'><div style='height:{round(40 * (1 - alto(p)))}px;line-height:0;font-size:0'>&nbsp;</div>"
            f"<div style='height:6px;line-height:0;font-size:0;border-radius:3px;background:{'#157a3a' if p == lo else '#0b6bcb'}'>&nbsp;</div></td>"
            for d, p in puntos) + "<td></td>" * (14 - len(puntos))
        h += (f"<p style='margin:14px 0 4px'><b>{html.escape(NOMBRES[k][0])}</b><br><span style='color:#666;font-size:13px'>"
              f"hoy {plata(hoy)} · mínimo {plata(lo)} (el {dia_lo}) · máximo {plata(hi)}</span></p>"
              f"<table style='width:100%;table-layout:fixed;border-collapse:collapse;background:#f4f6f8'><tr style='height:54px'>{celdas}</tr></table>"
              f"<span style='color:#888;font-size:12px'>{dia(puntos[0][0])}" + (f" → {dia(puntos[-1][0])}" if len(puntos) > 1 else "") + "</span>")
        g.append(f"• {html.escape(NOMBRES[k][1])}: " + ("en su mínimo" if hoy <= lo else f"{100 * (hoy - lo) / lo:.0f}% arriba del mínimo ({plata(lo)}, el {dia_lo})"))
    if not h:
        return "", ""
    return ("<p style='margin:22px 0 0;color:#666;font-size:13px;text-transform:uppercase;letter-spacing:.04em'>Cómo vienen los precios</p>"
            "<span style='color:#888;font-size:12px'>El más barato de cada día; arriba es más caro. En verde, el mínimo.</span>" + h,
            "\n<b>Contra el mínimo que vi</b>\n" + "\n".join(g))


def armar(novedades, actuales, rivales, codigos_txt, ejemplo=False, base=False, socio=None, diario=False, historia=None):
    """Arma el aviso. novedades = [(tipo, texto, link o None)]. Devuelve (asunto, texto, html_mail, html_telegram)."""
    e = html.escape
    filas = [(NOMBRES[k][0], actuales[k], rivales.get(k)) for k in NOMBRES if k in actuales and not k.startswith("socio")]
    total = sum(o["precio"] for t in TRAMOS if (o := elegido(actuales, t)))
    club = SOCIO["nombre"] if SOCIO else ""
    pct = f"{SOCIO['descuento']:.0%}" if SOCIO else ""
    quieto = base or not novedades   # sin novedad que titular: primera corrida o resumen diario
    titulo = (("resumen del día, " + (f"desde {plata(socio[0])} con {club}" if socio and socio[0] < total else f"total {plata(total)}"))
               if diario else "precios de hoy") if quieto else novedades[0][1]
    graf_mail, graf_tg = grafico(historia, {**{k: o["precio"] for k, o in actuales.items()}, **({"socio|mas barato": socio[0]} if socio else {})}) if historia else ("", "")
    asunto = ("[EJEMPLO] " if ejemplo else "") + "Pasajes: " + titulo[0].lower() + titulo[1:]
    if len(novedades) > 1 and not quieto:
        asunto += f" (+{len(novedades) - 1} más)"
    mostrar, resto = novedades[:6], len(novedades) - 6
    aclara = "EJEMPLO: las bajas son inventadas; los precios de la tabla son reales." if ejemplo else ""

    # Texto plano (respaldo del mail)
    t = [aclara] if aclara else []
    t += [f"{ICONOS[tp]} {tx}" for tp, tx, _ in mostrar]
    if resto > 0:
        t.append(f"... y {resto} más")
    t.append("\nHOY LO MÁS BARATO")
    for nombre, o, r in filas:
        t.append(f"\n{nombre}\n  {o['empresa']}, {cuando(o)}: {plata(o['precio'])}" + (f" con código {o['cupon']}" if o.get("cupon") else "")
                 + f"\n  Comprar: {o['link']}" + (f"\n  ({r['empresa']}: {plata(r['precio'])})" if r else ""))
    socio_txt = ""
    if socio:
        socio_txt = (f"Con {club} (estimado): {plata(socio[0])}. Ida y vuelta por Colonia Express "
                     f"({cuando(socio[2])} y {cuando(socio[3])}) con {pct} en pasajes; ahorra {plata(socio[1])}.")
    t.append(f"\nTotal: {plata(total)}" + (f"\n{socio_txt}" if socio_txt else "") + f"\n\nCódigos: {codigos_txt}")
    texto = "\n".join(t)

    # Mail en HTML: qué cambió arriba, una fila por tramo con el botón de compra
    borde = "padding:12px 8px;border-top:1px solid #e3e3e3;vertical-align:top"
    li = "".join(f"<li style='margin:6px 0'>{ICONOS[tp]} {e(tx)}" + (f" &nbsp;<a href='{e(lk)}'>ver</a>" if lk else "") + "</li>" for tp, tx, lk in mostrar)
    if resto > 0:
        li += f"<li style='margin:6px 0;color:#666'>… y {resto} más</li>"
    trs = ""
    for nombre, o, r in filas:
        chico = e(o["tarifa"]) + (f" · con código <b>{e(o['cupon'])}</b>" if o.get("cupon") else "") + (" · <b>quedan pocos</b>" if o.get("quedan_pocos") else "")
        if r:
            chico += f"<br>{e(r['empresa'])}: {plata(r['precio'])}"
        trs += (f"<tr><td style='{borde}'><b>{e(nombre)}</b><br>{e(o['empresa'])} · {e(cuando(o))}"
                f"<br><span style='color:#666;font-size:13px'>{chico}</span></td>"
                f"<td style='{borde};text-align:right;white-space:nowrap'><b style='font-size:18px'>{plata(o['precio'])}</b>"
                f"<br><a href='{e(o['link'])}' style='display:inline-block;margin-top:6px;padding:6px 12px;background:#0b6bcb;color:#fff;"
                f"text-decoration:none;border-radius:6px;font-size:14px'>Comprar</a></td></tr>")
    html_mail = (
        "<div style='font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:560px;margin:auto;color:#1a1a1a;font-size:15px;line-height:1.4'>"
        + (f"<p style='background:#fff3cd;padding:8px 10px;border-radius:6px'>{e(aclara)}</p>" if aclara else "")
        + (f"<ul style='padding-left:20px;margin:8px 0 18px'>{li}</ul>" if li else "")
        + "<p style='margin:0 0 4px;color:#666;font-size:13px;text-transform:uppercase;letter-spacing:.04em'>Hoy lo más barato</p>"
        + f"<table style='width:100%;border-collapse:collapse'>{trs}"
        + f"<tr><td style='{borde}'><b>Total</b></td><td style='{borde};text-align:right'><b style='font-size:18px'>{plata(total)}</b></td></tr>"
        + (f"<tr><td style='{borde}'><b>Con {e(club)}</b> (estimado)<br><span style='color:#666;font-size:13px'>"
           f"Ida y vuelta por Colonia Express, {e(cuando(socio[2]))} y {e(cuando(socio[3]))}, con {pct} en pasajes. Ahorra {plata(socio[1])}."
           f"</span></td><td style='{borde};text-align:right'><b style='font-size:18px;color:#157a3a'>{plata(socio[0])}</b></td></tr>" if socio else "")
        + "</table>" + graf_mail
        + f"<p style='margin-top:16px'>🏷️ <b>Códigos:</b> {e(codigos_txt)}</p>"
        + "<p style='color:#888;font-size:12px'>Precios en pesos, finales con tasas. El de Buquebus se calcula sumando sus tasas: confirmalo en el pago.</p></div>")

    # Telegram
    g = ([f"<i>{e(aclara)}</i>"] if aclara else []) + [f"{ICONOS[tp]} {e(tx)}" for tp, tx, _ in mostrar]
    if resto > 0:
        g.append(f"… y {resto} más")
    g.append("\n<b>Hoy lo más barato</b>")
    for nombre, o, r in filas:
        g.append(f"• <b>{e(nombre)}</b>\n   {e(o['empresa'])}, {e(cuando(o))}: <b>{plata(o['precio'])}</b> — <a href=\"{e(o['link'])}\">comprar</a>")
    g.append(f"\nTotal: <b>{plata(total)}</b>" + (f"\n💳 {e(socio_txt)}" if socio_txt else "") + f"\n🏷️ {e(codigos_txt)}")
    if graf_tg:
        g.append(graf_tg)
    return asunto, texto, html_mail, "\n".join(g)


def avisar(asunto, texto, html_mail=None, html_tg=None):
    cfg = {}
    if (AQUI / "config.json").exists():
        cfg = json.loads((AQUI / "config.json").read_text())
    enviado = False
    # Cada canal por separado: si uno falla (clave mal puesta, sin red), el otro igual sale.
    tg = cfg.get("telegram")
    if tg:
        try:
            cuerpo = {"chat_id": tg["chat_id"], "text": f"{asunto}\n\n{texto}"[:4000]}
            if html_tg:
                cuerpo = {"chat_id": tg["chat_id"], "parse_mode": "HTML", "disable_web_page_preview": True,
                          "text": f"<b>{html.escape(asunto)}</b>\n\n{html_tg}"[:4000]}
            http(f"https://api.telegram.org/bot{tg['token']}/sendMessage", cuerpo)
            enviado = True
        except Exception as e:
            print(f"Telegram falló: {e}", file=sys.stderr)
    m = cfg.get("mail")
    if m:
        try:
            msg = EmailMessage()
            msg["Subject"], msg["From"], msg["To"] = asunto, m["usuario"], m.get("para", m["usuario"])
            msg.set_content(texto)
            if html_mail:
                msg.add_alternative(html_mail, subtype="html")
            with smtplib.SMTP_SSL(m.get("servidor", "smtp.gmail.com"), 465) as s:
                s.login(m["usuario"], m["clave_app"].replace(" ", ""))
                s.send_message(msg)
            enviado = True
        except Exception as e:
            print(f"Mail falló: {e}", file=sys.stderr)
    (AQUI / "ultimo_aviso.txt").write_text(f"{asunto}\n\n{texto}\n")
    if html_mail:
        (AQUI / "ultimo_aviso.html").write_text(html_mail)
    if not enviado:
        print(f"\n=== {asunto} ===\n{texto}\n(sin Telegram ni mail configurados: aviso solo en la Mac)")
        # Notificación de macOS; el detalle queda en ultimo_aviso.txt
        if shutil.which("osascript"):
            subprocess.run(["osascript", "-e", 'display notification "Detalle en ultimo_aviso.txt" with title '
                           + json.dumps(asunto, ensure_ascii=False) + ' sound name "Glass"'], check=False)


# --- Base ---------------------------------------------------------------------
# El repositorio es público: el historial se sube cifrado (historial.enc) con "historial_clave" de config.json.
DB, ENC = AQUI / "historial.db", AQUI / "historial.enc"


def _openssl(clave, *args):
    subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256",
                    "-pass", "env:HISTORIAL_CLAVE", *map(str, args)], check=True, env={**os.environ, "HISTORIAL_CLAVE": clave})


def cifrar():
    if clave := _cfg().get("historial_clave"):
        _openssl(clave, "-salt", "-in", DB, "-out", ENC)


def abrir():
    clave = _cfg().get("historial_clave")
    if clave and ENC.exists() and (not DB.exists() or ENC.stat().st_mtime > DB.stat().st_mtime):
        _openssl(clave, "-d", "-in", ENC, "-out", DB)   # la versión cifrada es más nueva (vino de la nube)
    db = sqlite3.connect(DB)
    db.executescript("""
      CREATE TABLE IF NOT EXISTS precios(ts, empresa, tramo, fecha, hora, tarifa, precio, cupon);
      CREATE TABLE IF NOT EXISTS mejores(clave PRIMARY KEY, precio, detalle, ts);
      CREATE TABLE IF NOT EXISTS vistos(clave PRIMARY KEY, valor, ts);
    """)
    return db


def revisar(ejemplo=False, diario=False):
    db, ahora, hoy = abrir(), datetime.now().isoformat(timespec="minutes"), date.today()
    novedades, errores = [], []   # novedades = [(tipo, texto, link)]

    def nuevo(clave, valor=""):
        """True la primera vez que se ve esta clave con este valor; la deja registrada."""
        previo = db.execute("SELECT valor FROM vistos WHERE clave=?", (clave,)).fetchone()
        db.execute("INSERT OR REPLACE INTO vistos VALUES(?,?,?)", (clave, valor, ahora))
        return previo is None or previo[0] != valor

    # 1) Códigos de Colonia Express: se avisa solo cuando uno incluye tus fechas
    codigos = {}
    try:
        codigos = ce_codigos()
    except Exception as e:  # un sitio caído no debe frenar el resto
        errores.append(f"códigos Colonia Express: {e}")
    ce_aplican = []
    for cod, info in codigos.items():
        if info["aplica"]:
            ce_aplican.append(f"{cod} ({info['titulo']}) vale para {', '.join(f[:5] for f in info['aplica'])}")
        if nuevo(f"ce-cod|{cod}", json.dumps(info["aplica"])) and info["aplica"]:
            novedades.append(("codigo", f"Código {cod} de Colonia Express ({info['titulo']}) vale para {', '.join(f[:5] for f in info['aplica'])}", info["url"]))

    # 2) Cupones de Buquebus: se avisa cuando aparece uno de pasajes o bodega que todavía se puede usar
    bqb_vigentes = []
    try:
        for cod, info in bqb_cupones().items():
            sirve = not re.search(NO_SIRVEN, cod + " " + info["titulo"])
            vigente = info["vence"] is not None and info["vence"] >= hoy  # sin fecha clara = promo vieja del archivo
            if sirve and vigente:
                bqb_vigentes.append(f"{cod} ({info['titulo']}; {info['compra'] or 'sin fecha de compra'})")
            if nuevo(f"bqb-cod|{cod}|{info['vence']}") and sirve and vigente:
                novedades.append(("codigo", f"Cupón {cod} de Buquebus: {info['titulo']}. {info['compra']}".strip(), "https://www.buquebus.com/ar/bases-y-condiciones"))
    except Exception as e:
        errores.append(f"cupones Buquebus: {e}")
    codigos_txt = ("Colonia Express: " + ("; ".join(ce_aplican) if ce_aplican else f"{len(codigos)} publicados, ninguno para tus fechas")
                   + ". Buquebus: " + ("; ".join(bqb_vigentes) if bqb_vigentes else "ningún cupón de pasajes o bodega vigente")
                   + ". Cada cupón de Buquebus excluye fechas alrededor de las fiestas: miralas en sus bases antes de comprar.")

    # 3) Notas nuevas del blog que cubre las promos de los ferris
    try:
        for titulo in notas_blog():
            if nuevo(f"promo|Nota en Sir Chandler|{titulo[:200]}"):
                novedades.append(("nota", f"Nota nueva en Sir Chandler: {titulo}", None))
    except Exception as e:
        errores.append(f"blog: {e}")

    # 4) Precios de las dos empresas (Colonia Express también con cada código publicado)
    mejores = dict(db.execute("SELECT clave, precio FROM mejores"))
    primera_vez = not mejores
    firmados = {c: f for c in codigos if (f := ce_firmar(c))}
    actuales, rivales = {}, {}
    for tramo in TRAMOS:
        opciones = []
        for cupon, firmado in [(None, None), *firmados.items()]:
            try:
                ops = opciones_ce(tramo, cupon, firmado)
            except Exception as e:
                errores.append(f"precios {tramo['id']} ({cupon or 'sin código'}): {e}")
                continue
            if cupon:  # solo cuenta si el código realmente bajó algo
                base = {(o["fecha"], o["hora"], o["tipo"]): o["precio"] for o in opciones if not o["cupon"]}
                ops = [o for o in ops if o["precio"] < base.get((o["fecha"], o["hora"], o["tipo"]), 0)]
            opciones += ops
        try:
            opciones += opciones_bqb(tramo)
        except Exception as e:
            errores.append(f"precios Buquebus {tramo['id']}: {e}")
        db.executemany("INSERT INTO precios VALUES(?,?,?,?,?,?,?,?)",
                       [(ahora, o["empresa"], o["tramo"], o["fecha"], o["hora"], o["tarifa"], o["precio"], o["cupon"]) for o in opciones])
        ganadores = candidatos(tramo, opciones)
        actuales.update(ganadores)
        for clave, o in ganadores.items():  # el mejor de la otra empresa, para comparar en el aviso
            otro = candidatos(tramo, [x for x in opciones if x["empresa"] != o["empresa"]]).get(clave)
            if otro:
                rivales[clave] = otro

    # 4b) Plan con membresía: se calcula siempre y compite como una opción más
    socio, todos = None, dict(actuales)
    if actuales and SOCIO:
        try:
            resto = sum(o["precio"] for t in TRAMOS if t["id"] not in (SOCIO["ida"], SOCIO["vuelta"]) and (o := elegido(actuales, t)))
            socio = con_membresia(resto)
            todos["socio|mas barato"] = dict(socio[2], precio=socio[0], tarifa=f"con {SOCIO['nombre']}", cupon=None, quedan_pocos=False, link=None)
            db.execute("INSERT INTO precios VALUES(?,?,?,?,?,?,?,?)", (ahora, "Colonia Express", "socio", socio[2]["fecha"], socio[2]["hora"], SOCIO["nombre"], socio[0], None))
        except Exception as e:
            errores.append(f"estimado con membresía: {e}")

    # 5) Qué cambió: bajas respecto del mínimo, subas respecto de la revisión anterior, pocos lugares
    comparar = {k: v * 1.1 for k, v in mejores.items()} if ejemplo else mejores  # ejemplo: mínimos inflados 10%
    cambios = []
    for clave, o, antes in bajas(comparar, todos):
        cambios.append(("baja", f"{NOMBRES[clave][1]} bajó a {plata(o['precio'])} ({100 * (antes - o['precio']) / antes:.0f}% menos que {plata(antes)})", o["link"]))
    for clave, o in todos.items():
        ultimo = db.execute("SELECT valor FROM vistos WHERE clave=?", (f"ultimo|{clave}",)).fetchone()
        if ultimo and o["precio"] > float(ultimo[0]) * 1.03:
            cambios.append(("suba", f"{NOMBRES[clave][1]} subió a {plata(o['precio'])} (antes {plata(float(ultimo[0]))})", o["link"]))
        db.execute("INSERT OR REPLACE INTO vistos VALUES(?,?,?)", (f"ultimo|{clave}", str(o["precio"]), ahora))
        if o.get("quedan_pocos") and nuevo(f"pocos|{clave}|{o['empresa']}|{o['fecha']}|{o['hora']}"):
            cambios.append(("pocos", f"Quedan pocos lugares: {NOMBRES[clave][1].lower()}, {o['empresa']} {cuando(o)}", o["link"]))
        if clave not in mejores or o["precio"] < mejores[clave]:
            db.execute("INSERT OR REPLACE INTO mejores VALUES(?,?,?,?)", (clave, o["precio"], linea(o), ahora))
    novedades = cambios + novedades
    # Resumen diario: una vez por día, en la primera revisión desde las 9, haya o no novedades
    toca = bool(actuales) and not ejemplo and (diario or (datetime.now().hour >= 9 and nuevo("resumen-diario", str(hoy))))
    db.commit()
    cifrar()

    if actuales and (primera_vez or novedades or toca):
        avisar(*armar(novedades, actuales, rivales, codigos_txt, ejemplo=ejemplo, base=primera_vez, socio=socio, diario=toca, historia=series(db)))
    else:
        print(f"{ahora} sin novedades")
    if errores:
        print("Errores:\n  " + "\n  ".join(errores), file=sys.stderr)
    if not actuales:  # ninguna empresa devolvió precios: que la corrida figure como fallida
        sys.exit("No se obtuvo ningún precio en esta corrida")


def probar():
    """Manda un aviso de prueba por los canales configurados."""
    avisar("Monitor de pasajes: prueba", "Si te llegó esto, los avisos funcionan.")


def chatid():
    """Muestra el chat_id de quien le escribió al bot (hay que mandarle un mensaje antes)."""
    token = json.loads((AQUI / "config.json").read_text())["telegram"]["token"]
    chats = {(u["message"]["chat"]["id"], u["message"]["chat"].get("first_name", "")) for u in
             json.loads(http(f"https://api.telegram.org/bot{token}/getUpdates"))["result"] if "message" in u}
    print("\n".join(f"chat_id {i}  ({n})" for i, n in chats) or "Sin mensajes: escribile algo al bot en Telegram y repetí.")


def resumen():
    db = abrir()
    print("Mínimos registrados:")
    for clave, precio, detalle, ts in db.execute("SELECT * FROM mejores ORDER BY clave"):
        print(f"  {clave.replace('|', ', ')}: {detalle}  (visto {ts})")
    print("\nMás barato por tramo en cada revisión:")
    for ts, tramo, p in db.execute("SELECT ts, tramo, MIN(precio) FROM precios GROUP BY ts, tramo ORDER BY tramo, ts"):
        print(f"  {ts}  {tramo:<11} {plata(p)}")


def test():
    cargar_viaje({"tramos": [dict(id="ida", nombre="Ida (2 + auto)", corto="La ida", ruta="BUECYR", pax=2, auto=True, fechas={})],
                  "membresia": {"nombre": "Clarín 365 o La Nación", "descuento": 0.25, "ida": "ida", "vuelta": "ida"}})
    t = dict(id="x", flex=True)
    ops = [dict(precio=100, flex=False), dict(precio=150, flex=True), dict(precio=140, flex=True)]
    c = candidatos(t, ops)
    assert c["x|mas barato"]["precio"] == 100 and c["x|con cambio de fecha"]["precio"] == 140
    assert bajas({}, c) == []                                    # primera vez: línea de base, sin aviso
    assert bajas({"x|mas barato": 100, "x|con cambio de fecha": 140}, c) == []   # igual: sin aviso
    assert [b[0] for b in bajas({"x|mas barato": 120, "x|con cambio de fecha": 140}, c)] == ["x|mas barato"]
    assert plata(231500.0) == "$231.500"
    # tarifa 100 + tasa 20 por pasajero, 2 pasajeros, más un auto de 50 => 290
    pp = {"pax": [{"m_U258_NumberOfPassengers": "2", "c_PRIC_PriceDetails": [
              {"c_C084_Type": "PRODUCT", "m_C088_UnitGrossAmount": "100"}, {"c_C084_Type": "SURCHARGE", "m_C088_UnitGrossAmount": "20"},
              {"c_C084_Type": "DISCOUNT", "m_C088_UnitGrossAmount": "999"}]}],
          "veh": {"c_PRIC_PriceDetails": [{"c_C084_Type": "PRODUCT", "m_C088_UnitGrossAmount": "50"}]}}
    assert _bruto(pp) == 290
    # cupones: código en el mismo texto o en el siguiente, con fecha de compra
    cup = cupones_de(["CYBER MONDAY - 30% OFF", "CUPÓN: CiBER30", "PARA COMPRAS DEL 02/11/2026 AL 04/11/2026 INCLUSIVE",
                      "Para compras del 5 al 6/11/2026", "Promo Bodega", "cupón:", "XTRA10",
                      "25% OFF A URUGUAY | CUPÓN: VERANO", "PARA COMPRAS DEL 21/10/2026 AL 23/10/2026 | VIAJES DESDE EL 24/10/2026 AL 30/04/2027",
                      "CUPÓN VÁLIDO PARA CANJEAR POR ÚNICA VEZ"])
    assert cup["VERANO"]["vence"] == date(2026, 10, 23) and set(cup) == {"CIBER30", "XTRA10", "VERANO"}
    assert cup["CIBER30"]["vence"] == date(2026, 11, 4) and cup["CIBER30"]["titulo"] == "CYBER MONDAY - 30% OFF"
    assert cup["XTRA10"]["vence"] == date(2026, 11, 6) and cup["XTRA10"]["titulo"] == "Promo Bodega"
    # el aviso se arma, lleva el link de compra y el total
    o = dict(empresa="Colonia Express", fecha="2027-01-09", hora="08:30", tarifa="Super Economica", precio=231500.0,
             cupon=None, quedan_pocos=False, link="https://ejemplo/x?a=1&b=2")
    asunto, texto, h, g = armar([("baja", "La ida bajó a $231.500 (7% menos que $248.900)", o["link"])], {"ida|mas barato": o}, {}, "nada")
    assert asunto == "Pasajes: la ida bajó a $231.500 (7% menos que $248.900)"
    assert "sáb 9/1 08:30" in texto and "a=1&amp;b=2" in h and "Total: <b>$231.500</b>" in g
    # resumen diario sin novedades: título propio y gráfico con el mínimo marcado
    hist = {"ida|mas barato": [("2026-11-13", 235000.0), ("2026-11-14", 231500.0), ("2026-11-15", 240000.0)]}
    asunto, texto, h, g = armar([], {"ida|mas barato": o}, {}, "nada", diario=True, historia=hist)
    assert asunto == "Pasajes: resumen del día, total $231.500"
    assert h.count("#157a3a") == 1 and "mínimo $231.500 (el 14/11)" in h and "La ida: en su mínimo" in g
    assert "6% arriba del mínimo ($231.500, el 14/11)" in grafico(hist, {"ida|mas barato": 245400.0})[1]
    assert grafico({"ida|mas barato": []}) == ("", "")
    # con membresía más barato: lo dice el título y no aparece como fila de tramo
    asunto, texto, h, g = armar([], {"ida|mas barato": o, "socio|mas barato": o}, {}, "nada", diario=True, socio=(200000.0, 31500.0, o, o))
    assert asunto == "Pasajes: resumen del día, desde $200.000 con Clarín 365 o La Nación" and h.count("Comprar") == 1
    # cifrado de ida y vuelta
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        a, b, c = (Path(d) / n for n in "abc")
        a.write_bytes(b"historial")
        _openssl("clave", "-salt", "-in", a, "-out", b)
        _openssl("clave", "-d", "-in", b, "-out", c)
        assert c.read_bytes() == b"historial" and b.read_bytes() != b"historial"
    print("ok")


if __name__ == "__main__":
    {"--resumen": resumen, "--test": test, "--probar": probar, "--chatid": chatid,
     "--ejemplo": lambda: revisar(ejemplo=True), "--diario": lambda: revisar(diario=True)}.get(sys.argv[1] if len(sys.argv) > 1 else "", revisar)()
