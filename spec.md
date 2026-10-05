# Spec: Sendspin Bridge — puente AirPlay 1 → Sendspin (Python)

## Objetivo

Un destino AirPlay 1 por altavoz Sendspin individual expuesto, pareja estéreo y
grupo configurado. mDNS, `config.xml` persistente y un editor web para cambiarlo
visualmente. No soporta AirPlay 2.

## Runtime

```text
iPhone ─RAOP─> libraop (PCM S16LE estéreo, 44.1 kHz)
              └─> extensión CFFI / buffer nativo acotado
                    └─> scheduler Python, rejilla de 20 ms
                          └─> aiosendspin PushStream ─WS─> altavoces
```

- `libraop` sigue siendo el único receptor/decoder RAOP. El parche entrega PCM
  ordenado directamente; Python no implementa RAOP.
- La extensión CFFI copia PCM en un anillo nativo de dos segundos y expone
  eventos PLAY/FLUSH/STOP/volumen. Así no hay callbacks Python en el hilo de
  audio de libraop.
- `aiosendspin==9.1.1` es la implementación oficial de Sendspin. Un único
  `SendspinServer` anuncia `_sendspin-server._tcp`, descubre `_sendspin._tcp`
  y mantiene las conexiones de entrada y salida.
- El scheduler genera chunks de 20 ms. Cuando hay varios altavoces activos les
  da el mismo `play_start_us`; si no llega PCM a tiempo, rellena con silencio.
- Un listener HTTP interno en `:8080` sirve el bundle Preact/Tailwind
  precompilado. Su token fijo está compilado en la UI; no se debe exponer fuera
  de una LAN de confianza. El volumen individual se aplica en vivo a los
  altavoces conectados y no se escribe en XML. Guardar valida el XML y reinicia
  ordenadamente el bridge, no cambia targets en caliente.

## Configuración

`config.xml` se crea y actualiza atómicamente. Los puertos se asignan en bloques
de diez desde 7000.

```xml
<sendspin-bridge version="1" exposed_suffix=" (Sendspin)">
  <speakers>
    <speaker id="cocina" client_id="…" exposed_name="Cocina"
             direction="outbound" port="7000" exposed="true" delay_ms="0">
      <endpoint instance="…" host="192.168.1.50" port="8928" path="/sendspin"/>
    </speaker>
    <speaker id="salon" direction="inbound" port="7010" exposed="false">
      <endpoint path="/sendspin"/>
    </speaker>
  </speakers>
  <stereos>
    <stereo id="pareja" exposed_name="Sala estéreo" port="7020"
            left_id="cocina" right_id="salon"/>
  </stereos>
  <groups>
    <group id="casa" exposed_name="Toda la casa" port="7030">
      <speaker id="cocina"/><speaker id="salon"/>
    </group>
  </groups>
</sendspin-bridge>
```

- `outbound`: el bridge descubre o marca al reproductor Sendspin.
- `inbound`: el reproductor descubre y marca al bridge. `client_id` es la
  identidad que evita que un altavoz use ambos sentidos.
- `exposed="false"` no anuncia el target individual. Una pareja suspende los
  dos targets individuales aunque sus flags `exposed` sean `true`; el XML retiene
  las preferencias y las recupera al quitar la pareja. Los altavoces físicos
  permanecen en `<speakers>` para descubrimiento, volumen y retardo.
- Cada `<stereo>` anuncia un target propio salvo con `exposed="false"`; oculta,
  la pareja sigue sonando L/R en sus grupos y con un solo volumen. Usa dos
  altavoces distintos y enruta L/R duplicando cada lado en ambos canales de salida. Los grupos incluyen la
  pareja listando sus dos IDs de altavoz; un solo lado se rechaza. Si falta un
  miembro conectado, el restante recibe la mezcla estéreo completa.
- `delay_ms` está incluido en `[-500, 500]` y afecta sólo al audio de grupos y parejas:
  positivo lo retiene, negativo lo adelanta. El primer hello materializa `0`.

## Grupos y volumen

Cada `<group>` y `<stereo>` tiene su propio receptor AirPlay. Su PCM se guarda
por índice de chunk; cada miembro mezcla su copia (L, R o estéreo) con la
entrada individual si no está emparejado, usando saturación S16. El offset de cada miembro se aplica
por fotogramas completos antes de mezclar, sin permutar L/R. El volumen mueve la
media de los miembros sin borrar su diferencia, limitado a 0–100. Las dos mitades
de una pareja comparten volumen: al estar ambas conectadas toman el menor, y la
pareja, cualquiera de sus miembros, su emisor AirPlay o los botones de un altavoz
mueven las dos. Tras conectar o recibir un comando, los informes de volumen de un
altavoz se ignoran durante `VOLUME_SETTLE_S` (2 s) para no reenviar ecos.

No se usan grupos internos de `aiosendspin`: un `PushStream` nativo no mezcla
entradas concurrentes y sustituiría el target individual. La mezcla se hace
antes del stream para conservar audio individual + grupos y `delay_ms` firmado.

## Build y add-on

Docker compila el bundle Preact/Tailwind en una etapa Node, clona la revisión
fijada de libraop, aplica `patches/libraop/`, compila la extensión CFFI y
empaqueta Python 3.13 con `aiosendspin`. La etapa `runtime` usa
`/data/config.xml`; `addon` usa `/config/config.xml` y host networking.

El workflow publica una imagen multiarquitectura amd64, arm64 y arm/v7 al
cambiar `sendspin-bridge/config.yaml` en `main`.

## Comprobaciones mínimas

- CFFI abre y cierra un receptor RAOP real.
- XML conserva IDs, direcciones y `delay_ms` firmado.
- Un grupo retiene con delay positivo, adelanta con negativo y satura la mezcla.
- Una pareja reparte L/R, admite multiroom y evita desplazar medio fotograma estéreo.
- El build Docker ejecuta los tests Python, carga la extensión nativa y sirve
  la UI; un guardado de UI persiste el XML y reinicia el bridge.
