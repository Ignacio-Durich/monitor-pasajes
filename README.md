# Monitor de pasajes a Uruguay

Vigila los precios del ferry Buenos Aires ↔ Colonia en **Colonia Express** y **Buquebus**, junta los códigos de descuento que publican y avisa por **Telegram y mail** solo cuando conviene comprar: cuando un precio baja de su mínimo, sube, quedan pocos lugares o aparece un código que sirve para tus fechas.

Corre gratis en GitHub Actions cada 2 horas y guarda el historial de precios cifrado en el propio repositorio. Es un solo archivo de Python sin dependencias.

## Qué avisa

| Aviso | Cuándo |
|---|---|
| 🔻 Baja | Un tramo queda más barato que el mínimo registrado |
| 🔺 Suba | Un tramo sube más de 3% contra la revisión anterior |
| ⚠️ Quedan pocos | Colonia Express marca pocos lugares en la opción más barata |
| 🏷️ Código | Colonia Express publica un código que vale para tus fechas, o Buquebus un cupón de pasajes o bodega todavía vigente |
| 📰 Nota | Sale una nota nueva sobre promos de los ferris en [Sir Chandler](https://www.sirchandler.com.ar) |
| 📊 Resumen diario | Una vez por día, desde las 9, haya o no novedades |

Cada aviso trae lo más barato de cada tramo con un botón **Comprar** que abre la página de la empresa con la búsqueda ya cargada, el precio de la otra empresa para comparar, el total del viaje y cuánto está cada tramo sobre su mínimo. En el mail va además un gráfico con el precio más barato de cada día.

Así se ve en Telegram, con el viaje de ejemplo (precios inventados):

```
Pasajes: la vuelta bajó a $198.400 (7% menos que $213.900)

🔻 La vuelta bajó a $198.400 (7% menos que $213.900)

Hoy lo más barato
• Ida (2 + auto)
   Colonia Express, sáb 9/1 08:30: $231.500 — comprar
• Vuelta (2 + auto), con cambio de fecha
   Buquebus, sáb 16/1 19:15: $198.400 — comprar

Total: $429.900
💳 Con Clarín 365 o La Nación (estimado): $391.200 …
🏷️ Colonia Express: 4 publicados, ninguno para tus fechas. Buquebus: …

Contra el mínimo que vi
• La ida: 3% arriba del mínimo ($224.800, el 2/12)
• La vuelta con cambio: en su mínimo
```

## Cómo consigue los datos

- **Colonia Express:** el mismo buscador que usa su web (`book.coloniaexpress.com/api`). El precio que devuelve es final, con tasas. Los códigos salen de su página de beneficios, y cada uno se prueba contra tus tramos: solo cuenta si de verdad baja el precio.
- **Buquebus:** la consulta de disponibilidad de su web, con la sesión anónima que el sitio le da a cualquier visitante. El total que declara no incluye tasas, así que el precio se arma sumando tarifa y tasas de cada componente. Los cupones salen de sus páginas de inicio y de bases y condiciones.
- **Membresías (opcional):** si tenés un beneficio de socio en Colonia Express (Clarín 365 y Club La Nación dan 25% en pasajes de ida y vuelta), el monitor estima ese total en cada revisión y le sigue el mínimo como a un tramo más.

Todo es de lectura: el monitor no compra ni reserva nada.

## Ponerlo en marcha

1. **Hacé un fork** de este repositorio (o copialo a uno tuyo, privado si preferís).
2. **Creá un bot de Telegram:** escribile a [@BotFather](https://t.me/BotFather), mandá `/newbot` y guardá el token. Después escribile cualquier cosa a tu bot.
3. **Creá una contraseña de aplicación de Gmail** en [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (pide verificación en dos pasos).
4. **Armá `config.json`** a partir de `config.ejemplo.json`, con tus claves, tu viaje (ver abajo) y una frase larga al azar en `historial_clave`. Para averiguar tu `chat_id`, con el token ya cargado corré:

   ```bash
   python3 monitor.py --chatid
   ```

   En `para` podés poner varias direcciones separadas por comas. Cualquiera de los dos canales es opcional.
5. **Probá los avisos** en tu compu:

   ```bash
   python3 monitor.py --probar
   ```

6. **Cargá la configuración como secreto** del repositorio (nunca la subas como archivo):

   ```bash
   gh secret set CONFIG_JSON < config.json
   ```

7. **Activá Actions** en la pestaña *Actions* del fork. Desde ahí también podés lanzarlo a mano, con opciones para mandar una prueba, un ejemplo de baja o el resumen diario.

La primera revisión manda los precios de ese momento y los toma como base; desde ahí solo avisa cuando hay novedades.

## Tu viaje

El viaje va en `config.json`, no en el código, así que tus fechas y quiénes viajan no quedan en el repositorio:

```json
"viaje": {
  "tramos": [
    {"id": "ida", "nombre": "Ida (2 + auto)", "corto": "La ida", "ruta": "BUECYR",
     "pax": 2, "auto": true, "fechas": {"2027-01-09": "08:00", "2027-01-10": "00:00"}},
    {"id": "vuelta", "nombre": "Vuelta (2 + auto)", "corto": "La vuelta", "ruta": "CYRBUE",
     "pax": 2, "auto": true, "flex": true, "fechas": {"2027-01-16": "12:00"}}
  ],
  "fechas_codigos": ["09/01/2027", "10/01/2027", "16/01/2027"],
  "membresia": {"nombre": "Clarín 365 o La Nación", "descuento": 0.25, "ida": "ida", "vuelta": "vuelta"}
}
```

- `ruta`: `BUECYR` (Buenos Aires → Colonia) o `CYRBUE` (vuelta).
- `fechas`: cada día a revisar, con la hora mínima de salida.
- `pax` y `auto`: pasajeros adultos y si viaja un auto chico.
- `flex`: además del más barato, sigue la tarifa que permite cambiar la fecha, y esa es la que entra en el total.
- `nombre` y `corto`: cómo aparece el tramo en el aviso ("Ida (2 + auto)", "La ida bajó a…").
- `fechas_codigos`: los días contra los que se comparan los códigos publicados.
- `membresia` (opcional): los que viajan en el tramo `vuelta` compran ida y vuelta con descuento; si en la `ida` van más personas, el resto va con pasaje suelto en el mismo barco.

Cada tramo puede tener una cantidad distinta de pasajeros, y podés sumar los que quieras (por ejemplo, una vuelta abierta para parte del grupo).

## Comandos

```
python3 monitor.py            revisa y avisa solo si hay novedad
python3 monitor.py --diario   revisa y manda el resumen del día
python3 monitor.py --ejemplo  revisa y manda una baja simulada, marcada como ejemplo
python3 monitor.py --resumen  muestra los mínimos y el historial guardado
python3 monitor.py --probar   manda un aviso de prueba
python3 monitor.py --chatid   muestra tu chat_id de Telegram
python3 monitor.py --test     chequeo rápido de la lógica
```

Para lanzar una revisión en la nube desde la terminal:

```bash
gh workflow run monitor.yml -f diario=true
```

## Historial

El historial es una base SQLite que se sube cifrada (`historial.enc`, AES-256 con `historial_clave`), así que en un repositorio público nadie ve tus precios. El monitor la descifra solo al empezar si tiene la clave; en tu compu alcanza con `git pull` y `python3 monitor.py --resumen`.

Adentro hay tres tablas:

- `precios`: cada opción vista (empresa, tramo, fecha, hora, tarifa, precio, código).
- `mejores`: el mínimo registrado por tramo.
- `vistos`: códigos, notas y últimos precios ya avisados, para no repetir.

Si no ponés `historial_clave`, el historial se guarda sin cifrar.

## Limitaciones

- **No es oficial.** Usa las mismas consultas que las webs de las empresas; si las cambian, el monitor deja de traer precios hasta que se ajuste.
- **El precio de Buquebus es calculado.** Coincide con su desglose, pero confirmalo en la pantalla de pago.
- **El estimado con membresía no se prueba con un cupón real.** Aplica el descuento según las condiciones publicadas.
- **GitHub atrasa las tareas programadas.** Cuando tiene mucha carga, las corridas salen tarde o se saltean; por eso se piden cada 2 horas.
- **GitHub puede desactivar las tareas programadas** de un repositorio sin actividad por 60 días. Avisa por mail y se reactivan con un clic en la pestaña *Actions*.

Cuando ya compraste, apagalo:

```bash
gh workflow disable monitor.yml
```

## Licencia

[MIT](LICENSE)
