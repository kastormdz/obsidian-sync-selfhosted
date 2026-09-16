#!/usr/bin/env bash
# sensitive-scan.sh — evita publicar identificadores y credenciales en un repo
#
# Ciclo completo:
#   sensitive-scan.sh --check [--all|--staged] [--show-matches]
#   sensitive-scan.sh --install | --uninstall | --status
#   sensitive-scan.sh --update      (regenera los patrones locales de esta máquina)
#   sensitive-scan.sh --how
#
# Diseño: los patrones GENÉRICOS (claves, tokens, rutas home) viven acá y son
# publicables. Los patrones PERSONALES (tu hostname, tus dominios, tu IP, tu
# usuario) viven en un archivo LOCAL fuera del repo, así el hook nunca filtra lo
# que justamente existe para proteger.
#
# Exit code: 0 = limpio · 1 = hallazgos · 2 = error de uso.

set -uo pipefail

PATTERNS_FILE="${SENSITIVE_PATTERNS_FILE:-${XDG_CONFIG_HOME:-$HOME/.config}/git/sensitive-patterns}"
HOOKS_DIR=".githooks"
SHOW_MATCHES=0

# Patrones que NO dependen del entorno: se pueden publicar sin riesgo.
GENERIC_PATTERNS=(
  'ruta-home:/home/[A-Za-z0-9._-]+/'
  'clave-privada:-----BEGIN [A-Z ]*PRIVATE KEY'
  'aws-key:AKIA[0-9A-Z]{16}'
  'garage-key:GK[0-9a-f]{26}'
  'github-token:(ghp|gho|ghu|ghs)_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}'
  'openai-key:sk-(ant-)?[A-Za-z0-9_-]{20,}'
  'slack-token:xox[baprs]-[A-Za-z0-9-]{10,}'
  'jwt:eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.'
  'credencial-en-claro:(password|passphrase|secret|token|api[_-]?key)[[:space:]]*[:=][[:space:]]*"[^"<>]{8,}"'
)

# Rutas que no tiene sentido escanear: ejemplos con placeholders y este mismo script.
SKIP_PATHS=(
  ':(exclude)*.example'
  ':(exclude)*.md'
  ':(exclude).githooks/*'
  ':(exclude)scripts/sensitive-scan.sh'
)

how() {
  cat <<'TXT'
sensitive-scan — qué detecta y cómo

DOS FAMILIAS DE PATRONES
  1. Genéricos (en este script, publicables): claves privadas, AWS/Garage/GitHub/
     OpenAI/Slack, JWT, credenciales en claro entre comillas y rutas /home/<usuario>/.
  2. Personales (en un archivo LOCAL que nunca se commitea): tu hostname, tu
     usuario, tu IP de LAN, tu email de git y los dominios que uses.
     Archivo por defecto: ~/.config/git/sensitive-patterns
     (override con SENSITIVE_PATTERNS_FILE)

FORMATO DEL ARCHIVO DE PATRONES (una regla por línea)
  categoria:regex        → alerta si matchea
  !categoria:regex       → excepción: ignora ese caso

A DÓNDE MIRA
  --staged (default)  solo lo que está en el índice, o sea lo que vas a commitear
  --all               todos los archivos rastreados (útil antes de publicar un repo)

ENMASCARADO
  Por defecto el valor que matcheó se reemplaza por <oculto>, así el propio reporte
  del hook no filtra el dato en logs ni en capturas de pantalla. Con --show-matches
  se muestra crudo, para debug.

POR QUÉ NO HAY REGLA DE "ALTA ENTROPÍA"
  Una regla genérica de "string largo y random" da falsos positivos con los digests
  de imágenes (sha256:...) y con hashes en documentación. Los prefijos conocidos
  cubren los secretos reales sin romper el hook.

IPs DE EJEMPLO
  Usá el rango de documentación 192.0.2.0/24 (RFC 5737) en los ejemplos: nunca es
  una IP real, así que no dispara la regla de IP interna ni filtra topología ajena.

CÓMO SALTEARLO (a conciencia)
  SENSITIVE_SCAN_ALLOW=1 git commit -m "..."   → permite el commit igual

QUÉ NO DETECTA
  Secretos con formato propio sin prefijo conocido, ni datos sensibles que no
  matcheen ninguna regla. Es una red de seguridad, no un sustituto de revisar.
TXT
}

patterns_file_lines() {
  [ -f "$PATTERNS_FILE" ] && grep -vE '^[[:space:]]*(#|$)' "$PATTERNS_FILE" || true
}

cmd_update() {
  local dir; dir="$(dirname "$PATTERNS_FILE")"
  mkdir -p "$dir"
  umask 077

  # Semilla: identificadores REALES conocidos de esta infraestructura + los que
  # detecto automáticamente de la máquina. Este archivo es LOCAL: nunca se sube.
  {
    echo "# Patrones personales de ESTA máquina/infra. Archivo local: NO versionar."
    echo "# Formato: categoria:regex  ·  !categoria:regex para excepciones."
    echo "# Generado por sensitive-scan.sh --update el $(date '+%Y-%m-%d %H:%M')"
    echo
    echo "# ── identificadores conocidos de la infraestructura ──────────────────"
    echo 'dominio:cronix\.(com|ar)'
    echo 'hostname:titan|juno|yavin'
    echo 'ip-interna:(10|192\.168|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]{1,3}\.[0-9]{1,3}'
    echo 'ruta-host:/home/samba/'
    echo
    echo "# ── detectado automáticamente en esta máquina ────────────────────────"
    echo "usuario-local:$(id -un)"
    local esc_home; esc_home="$(printf '%s' "$HOME" | sed 's/[.[\*^$()+?{|]/\\&/g')"
    echo "home-local:${esc_home}"
    local h
    for h in "${HOSTNAME:-}" "$(hostname -s 2>/dev/null || true)"; do
      [ -n "$h" ] && echo "hostname:${h}"
    done
    local ip
    for ip in $(hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]'); do
      case "$ip" in 127.*|::1) continue ;; esac
      echo "ip-local:$(printf '%s' "$ip" | sed 's/\./\\./g')"
    done
    local em; em="$(git config --get user.email 2>/dev/null || true)"
    [ -n "$em" ] && echo "email-local:$(printf '%s' "$em" | sed 's/[.[\*^$()+?{|@]/\\&/g')"
    echo
    echo "# ── excepciones (empezar con !) ──────────────────────────────────────"
    echo '# !dominio:ejemplo-inofensivo\.com'
  } > "$PATTERNS_FILE"

  # sin duplicados, preservando orden
  awk '!seen[$0]++' "$PATTERNS_FILE" > "$PATTERNS_FILE.tmp" && mv "$PATTERNS_FILE.tmp" "$PATTERNS_FILE"
  chmod 600 "$PATTERNS_FILE"

  echo "✔ Patrones personales escritos en $PATTERNS_FILE ($(patterns_file_lines | grep -vc '^!') reglas activas, modo 600)"
  echo
  echo "  Agregá tus dominios a mano si falta alguno:"
  echo "    echo 'dominio:tudominio\\.com' >> $PATTERNS_FILE"
}

cmd_status() {
  echo "── sensitive-scan ───────────────────────────────"
  printf '  hook instalado   : %s\n' "$(git config --get core.hooksPath 2>/dev/null || echo 'NO (correr --install)')"
  printf '  patrones locales : %s\n' "$([ -f "$PATTERNS_FILE" ] && echo "$PATTERNS_FILE" || echo 'NO (correr --update)')"
  printf '  reglas personales: %s\n' "$([ -f "$PATTERNS_FILE" ] && patterns_file_lines | grep -vc '^!' || echo 0)"
  printf '  reglas genéricas : %s\n' "${#GENERIC_PATTERNS[@]}"
  echo "─────────────────────────────────────────────────"
}

# Enmascara el texto que matcheó reemplazándolo por <oculto>.
# Se busca el match con grep -o (regex, sin problemas de delimitador) y se
# reemplaza el LITERAL escapado: así una regex con / o | no rompe el sed.
mask_line() {
  local re="$1" txt="$2" lit esc
  lit="$(printf '%s' "$txt" | grep -oE "$re" | head -1)"
  if [ -z "$lit" ]; then printf '%s' "$txt" | cut -c1-160; return; fi
  esc="$(printf '%s' "$lit" | sed 's/[\\/&]/\\&/g')"
  printf '%s' "$txt" | sed "s/${esc}/<oculto>/g" | cut -c1-160
}

cmd_check() {
  local scope="${1:-staged}"
  local -a grep_args
  if [ "$scope" = "all" ]; then grep_args=(git grep -n -I -E); else grep_args=(git grep --cached -n -I -E); fi

  # Reglas activas y excepciones
  local -a rules=() exclusions=() spec
  for spec in "${GENERIC_PATTERNS[@]}"; do rules+=("$spec"); done
  while IFS= read -r spec; do
    [ -z "$spec" ] && continue
    case "$spec" in '!'*) exclusions+=("$spec") ;; *) rules+=("$spec") ;; esac
  done < <(patterns_file_lines)

  # Recolectar: una entrada por ubicación, acumulando categorías que matchearon
  local -A CATS=() ; local -A TEXT=() ; local -A RES=()
  local cat re out line file ln content key
  for spec in "${rules[@]}"; do
    cat="${spec%%:*}"; re="${spec#*:}"
    out="$("${grep_args[@]}" "$re" -- . "${SKIP_PATHS[@]}" 2>/dev/null || true)"
    [ -z "$out" ] && continue
    while IFS= read -r line; do
      [ -z "$line" ] && continue
      file="${line%%:*}"; content="${line#*:}"; ln="${content%%:*}"; content="${content#*:}"
      # excepciones
      local skip=0 e
      for e in ${exclusions[@]+"${exclusions[@]}"}; do
        [ -z "$e" ] && continue
        printf '%s' "$file:$content" | grep -qE "${e#*:}" && { skip=1; break; }
      done
      [ "$skip" = 1 ] && continue
      key="$file:$ln"
      case " ${CATS[$key]:-} " in
        *" $cat "*) ;;
        *) CATS[$key]="${CATS[$key]:-}$cat "
           RES[$key]="${RES[$key]:-}"$'\x1f'"$re" ;;
      esac
      TEXT[$key]="$content"
    done <<< "$out"
  done

  echo "▶ Escaneando ($scope) con ${#rules[@]} reglas…"
  if [ "${#CATS[@]}" -eq 0 ]; then echo "✔ limpio"; return 0; fi

  local k
  for k in $(printf '%s\n' "${!CATS[@]}" | sort); do
    printf '  ✘ [%s] %s\n' "$(printf '%s' "${CATS[$k]}" | sed 's/ $//' | tr ' ' ',')" "$k"
    if [ "$SHOW_MATCHES" = 1 ]; then
      printf '      %s\n' "$(printf '%s' "${TEXT[$k]}" | cut -c1-160)"
    else
      # enmascara con CADA regex que matcheó esa línea (separadas por \x1f)
      # OJO: no usar `tr '\037' '\n'` acá — se come el último elemento.
      local -a rxs=()
      IFS=$'\x1f' read -r -a rxs <<< "${RES[$k]}"
      local shown="${TEXT[$k]}" r
      for r in ${rxs[@]+"${rxs[@]}"}; do
        [ -z "$r" ] && continue
        shown="$(mask_line "$r" "$shown")"
      done
      printf '      %s\n' "$shown"
    fi
  done

  echo
  echo "✘ ${#CATS[@]} ubicación(es) con datos que no deberían publicarse."
  echo "  Arreglalo, o forzá el commit con: SENSITIVE_SCAN_ALLOW=1 git commit ..."
  return 1
}

cmd_install() {
  local root; root="$(git rev-parse --show-toplevel)"
  mkdir -p "$root/$HOOKS_DIR"
  cat > "$root/$HOOKS_DIR/pre-commit" <<'HOOK'
#!/usr/bin/env bash
# Instalado por scripts/sensitive-scan.sh --install
# La lógica vive en el script (un solo lugar, testeable).
exec "$(git rev-parse --show-toplevel)/scripts/sensitive-scan.sh" --check --staged
HOOK
  chmod +x "$root/$HOOKS_DIR/pre-commit"
  git config core.hooksPath "$HOOKS_DIR"
  echo "✔ hook instalado: core.hooksPath=$HOOKS_DIR"
  echo "  Se activa automáticamente en cada commit de ESTE clon."
  [ -f "$PATTERNS_FILE" ] || { echo; echo "  ⚠ No hay patrones personales todavía. Corré: $0 --update"; }
}

cmd_uninstall() {
  git config --unset core.hooksPath 2>/dev/null && echo "✔ hook desinstalado (los archivos quedan)" || echo "  no estaba instalado"
}

# ── argumentos ────────────────────────────────────────────────────────────────
action=""; scope="staged"
while [ $# -gt 0 ]; do
  case "$1" in
    --check) action="check" ;;
    --install) action="install" ;;
    --uninstall) action="uninstall" ;;
    --status) action="status" ;;
    --update) action="update" ;;
    --how|-h|--help) action="how" ;;
    --all) scope="all" ;;
    --staged) scope="staged" ;;
    --show-matches) SHOW_MATCHES=1 ;;
    *) echo "Opción desconocida: $1 (ver --how)" >&2; exit 2 ;;
  esac
  shift
done

case "${action:-how}" in
  check)     cmd_check "$scope" ;;
  install)   cmd_install ;;
  uninstall) cmd_uninstall ;;
  status)    cmd_status ;;
  update)    cmd_update ;;
  how)       how ;;
esac
