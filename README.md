# CUSTOM NEKO REPO
A collection of NEKO VOID templates for xbps-src
### x86_64 REPO
example

``` 
sudo xbps-install  -S --repository=https://github.com/Neko-Void-Linux/repo-neko/releases/download/stable steam-nk
```
### Run on void-packages for compile (goverlay-bin and mangowc-latest)
```
sh -c "cat << EOF >> common/shlibs
libscenefx-0.5.so scenefx-0.5_1
libQt6Pas.so.6 qt6pas-6.2.10_1
libcrypto.so.1.1 libssl1.1-1.1.1w_1
EOF"
```  
### FOR UPDATE OR ADD PACKAGES IN REPO RUN ACTION WORKFLOW  (USE GITHUB MIGRATION PLEASE) 
https://github.com/Neko-Void-Linux/repo-neko/actions/workflows/Migration.yml

## Templates automáticos desde tarballs

Este repositorio puede generar templates XBPS para binarios precompilados y
proyectos compilables que publiquen tarballs en GitHub Releases. La
automatización conserva el filtrado de Belka, pero añade comprobaciones
específicas para XBPS:

- descarta assets de Windows, macOS, BSD, ARM y otras arquitecturas;
- para recetas `binary`, exige un tarball Linux x86_64 y un ELF x86_64;
- para recetas `bundle`, conserva el arbol completo de la aplicacion, enlaza el
  ELF principal e instala sus archivos desktop e iconos cuando existen;
- para recetas `source`, exige un sistema de compilación soportado y las
  dependencias declaradas por el revisor;
- descarga con comprobación HTTP y calcula SHA256;
- rechaza archivos vacíos, corruptos, rutas inseguras, enlaces que escapen del
  archivo, dispositivos y archivos excesivamente grandes;
- nunca sobrescribe templates mantenidos manualmente.

Para añadir un paquete basta con pegar su enlace de GitHub en `meta/next.list`,
uno por línea. El workflow analiza la release, filtra los assets, detecta si es
un binario, bundle o código fuente y, si pasa la revisión, añade su receta a
`meta/tarballs.tsv` y genera el template. `next.list` es un registro permanente:
los enlaces aprobados y fallidos permanecen allí; los ya importados se omiten
sin duplicar sus recetas.

Formatos opcionales de `next.list`:

```text
https://github.com/owner/repo
https://github.com/owner/repo nombre-paquete
https://github.com/owner/repo nombre-paquete binary ejecutable
https://github.com/owner/repo nombre-paquete bundle ejecutable
https://github.com/owner/repo nombre-paquete source cmake
https://github.com/owner/repo nombre-paquete binary ejecutable --ignore-license
```

La opcion `--ignore-license` solo omite el bloqueo cuando GitHub no entrega una
licencia SPDX (`NOASSERTION` o `OTHER`). La receta conserva
`license="custom:unknown"` para indicar que la licencia debe revisarse
manualmente. Esta opcion no omite ninguna comprobacion del tarball.

Las recetas aprobadas de `meta/tarballs.tsv` contienen campos separados por
tabuladores:

```text
pkgname  repo  patron_asset  tipo  ejecutable  build_style  licencia  descripcion  mantenedor  hostmakedepends  makedepends  dependencias  configure_args
```

Ejemplo:

```text
bat  sharkdp/bat  ^bat-v.*-x86_64-unknown-linux-gnu\.tar\.gz$  binary  bat  -  Apache-2.0 OR MIT  Clone de cat con resaltado de sintaxis
```

Después de añadir y revisar una receta se puede ejecutar:

```sh
python3 scripts/generate-xbps-templates.py bat
scripts/check-generated-templates.sh
```

El workflow `Actualizar templates desde tarballs` se ejecuta automáticamente
cuando cambia `meta/next.list`, cada domingo y también de forma manual para uno
o más paquetes. Sólo los templates que contienen la marca de autogenerado
pueden ser actualizados por el bot. Los tarballs fuente admiten `cmake`,
`meson`, `gnu-configure` y
`gnu-makefile`. El workflow valida que exista la estructura correspondiente,
pero el revisor debe declarar `hostmakedepends`, `makedepends`, `depends` y
`configure_args`: esas decisiones no pueden deducirse de forma fiable sólo
mirando el archivo. En una receta `source`, el patrón especial `TARBALL` usa el
tarball automático de la etiqueta de GitHub cuando la release no publica un
asset de código fuente. Los programas con pasos personalizados que no encajen
en los modos anteriores deben seguir usando un template manual.
