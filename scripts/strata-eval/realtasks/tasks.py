"""Real tasks: each one reproduces a commit from the user's own repos.

The agent gets the repo at the commit's parent (history cut there, so `git log` cannot show
the answer), a prompt written the way the user would ask, and never sees the verifier.
verify.py checks the result; the real commit is the reference for the rubric.

Fields: repo, commit (the real one; the workspace is commit^), cat, diff (easy|medium|hard),
nix (the agent gets the nix daemon socket, the only task kind that does), prompt.
"""

REPOS = {
    # public
    "kubernetes": "https://github.com/agonbar/agonbar-kubernetes.git",
    "benilla": "https://github.com/agonbar/benilla.git",
    # private: cloned from the local checkout, never pushed anywhere by this harness
    "dotfiles": "~/dotfiles",
}

TAIL = ("\n\nNo tienes acceso a ningún clúster ni credenciales; trabaja sobre el repo. "
        "No hagas push. Al terminar, resume en pocas líneas qué cambiaste y cómo lo comprobaste.")

TASKS = {
    # ---------------------------------------------------------------- kubernetes (GitOps de lamg)
    "k-backup-deleted-pvc": dict(
        repo="kubernetes", commit="c92773d", cat="bugfix", diff="hard",
        prompt="Desde el 2026-09-08 el backup nocturno (system/snapshots/backup.yaml) no ha copiado ni un solo "
               "volumen del namespace piracy. El Job acaba en Failed y en el log, al llegar a los volúmenes de "
               "piracy, la salida se corta sin ningún mensaje de error. Los namespaces que van antes en la lista "
               "sí se copian. Encuentra la causa y arréglala. Un PVC de la lista que ya no exista tiene que "
               "saltarse con un aviso, sin marcar la noche como fallida ni cortar el resto de la copia."),
    "k-nas-diff-extra": dict(
        repo="kubernetes", commit="21de5cb", cat="feature", diff="medium",
        prompt="scripts/nas-public-diff.py compara dos listados de Public (nas03 y nas02). Muchos ficheros que salen "
               "como `unique` en realidad los moví de Public a datasets personales de nas02 (RAID/adrian, "
               "RAID/nas00-rescate). Quiero poder pasarle, después de los dos listados, listados extra de esos "
               "datasets (mismo formato `find -printf '%s\\t%T@\\t%p\\0'`, pero con rutas absolutas). Los extra "
               "solo sirven para encontrar dónde acabó un fichero de A (mismo nombre y tamaño): no cuentan como B "
               "para la comparación por ruta. `--dump DIR` tiene que seguir funcionando en cualquier posición. "
               "Actualiza el docstring."),
    "k-autotaint-deployment": dict(
        repo="kubernetes", commit="1f00583", cat="manifest", diff="medium",
        prompt="El CronJob de system/node-autotaint corre cada 2 minutos y cada ejecución crea y borra un pod. Cada "
               "cambio de pod hace que el controlador de network policy de k3s reconstruya iptables e ipsets en "
               "todos los nodos y que ArgoCD refresque la app unas 25 veces; en ovh02 eso frena el tick del "
               "servidor de Satisfactory 1-2 s en cada ejecución. Conviértelo en un Deployment de una réplica que "
               "haga la misma comprobación en bucle cada 120 s, sin crear pods nuevos. Que un fallo de una pasada "
               "no tumbe el bucle. El manifiesto tiene que pasar kubeconform -strict."),
    "k-seal-secrets-shrink": dict(
        repo="kubernetes", commit="b1ec6c7", cat="feature", diff="hard",
        prompt="Antes de volver a ejecutar seal-secrets.sh: en varios directorios el sealedsecrets.yaml tiene más "
               "secretos sellados que el secrets.yaml de al lado (los añadí directamente al fichero sellado, sin "
               "texto plano en local; secrets.yaml no está en git). Si ejecuto el script tal cual, pierdo esos "
               "secretos sin enterarme. Haz que el script se niegue a sustituir un sealedsecrets.yaml por uno con "
               "menos documentos SealedSecret, que lo diga y que termine con código distinto de 0. Además, si "
               "kubeseal falla, el sealedsecrets.yaml existente no puede quedarse vacío ni tocarse. Un directorio "
               "sin secrets.yaml se salta. Añade DRY_RUN=1 para ver qué haría con cada par sin escribir nada. "
               "kubeseal no está disponible aquí."),
    "k-snapshot-class-why": dict(
        repo="kubernetes", commit="3675b44", cat="investigate", diff="hard",
        prompt="Al restaurar tachidesk desde su VolumeSnapshot de anoche (que sale readyToUse), el PVC nuevo se "
               "queda Pending con `snapshot ... not bound`. Investiga en el repo por qué pasa y a qué volúmenes "
               "del backup nocturno (system/snapshots/backup.yaml) les pasaría lo mismo. No cambies ningún "
               "fichero: escribe la respuesta en ANSWER.md con la causa y, en la última línea, "
               "`AFFECTED: ns/pvc ns/pvc ...` con todos los volúmenes afectados de la lista del backup, "
               "ordenados alfabéticamente."),
    # ---------------------------------------------------------------- dotfiles (Nix, home-manager, scripts)
    "d-paperclip-env": dict(
        repo="dotfiles", commit="da75b00", cat="bugfix", diff="hard",
        prompt="La primera ejecución real de pi-paperclip (nix/home/modules/pi/paperclip-sandbox.sh, empaquetado "
               "con writeShellApplication en nix/home/modules/pi.nix) llegó a pi sin ninguna variable PAPERCLIP_*, "
               "así que el agente no podía hablar con la API. Paperclip sí las pone en el entorno del proceso. "
               "Encuentra por qué se pierden y arréglalo."),
    "d-guard-subcommand": dict(
        repo="dotfiles", commit="a7f00b8", cat="bugfix", diff="medium",
        prompt="El hook nix/home/modules/claude-hooks/kubectl-live-write-guard.sh bloquea como escritura en vivo "
               "comandos que no lo son. Ejemplos bloqueados contra paperclip (gestionado por Argo):\n"
               "  kubectl --context=sensei-ovh -n infra exec deploy/paperclip -- sh -c 'pc agent skills:sync x --mode replace'\n"
               "  kubectl --context=sensei-ovh -n infra exec deploy/paperclip -- sh -c 'set=a; grep -rn router.patch src'\n"
               "  kubectl --context=sensei-ovh -n infra exec deploy/paperclip -- node -e 'c=c.replace(a,b)'\n"
               "Arréglalo para que el verbo sea el subcomando de kubectl de verdad. Tiene que seguir bloqueando las "
               "escrituras reales, también si van en una segunda llamada a kubectl dentro del mismo comando. Añade "
               "esos casos a tests/kubectl-live-write-guard.sh. Ese test usa clústeres reales a los que no llegas; "
               "si quieres ejecutarlo, simula kubectl."),
    "d-whisper-newlines": dict(
        repo="dotfiles", commit="ee9abfe", cat="bugfix", diff="medium",
        prompt="Los dictados de voxtype que pasan por la puerta OpenAI de nix/pkgs/wyoming-whisper-bridge llegan "
               "partidos y voxtype envía el mensaje a medias, porque teclea un salto de línea como Enter. Ejemplos "
               "reales de lo que llega: \"¿Me lo\\n puedes probar?\" y hasta a mitad de palabra: \"escrib\\niendo.\". "
               "Arréglalo en el bridge, con tests en Go. El modo reunión pide verbose_json y sus segmentos no se "
               "pueden tocar."),
    "d-meeting-env-quote": dict(
        repo="dotfiles", commit="e1a7183", cat="nix", diff="medium", nix=True,
        prompt="El servicio meeting-bot-scheduler (nix/home/modules/meeting-bot.nix) se une a reuniones que no "
               "debería: con MEETING_ONLY = \"^Daily SenseiVision$\" el scheduler recibe solo \"^Daily\". Encuentra "
               "por qué y arréglalo en el módulo. Puedes comprobarlo con nix eval sobre "
               ".#nixosConfigurations.work-vm-00 (home-manager.users.agonbar) desde nix/."),
    "d-rebuild-nopasswd": dict(
        repo="dotfiles", commit="0cd88ef", cat="refactor", diff="hard", nix=True,
        prompt="Ayer le puse a t480 las reglas de sudo para que update.sh (el alias rebuild) funcione sin "
               "contraseña. asus-rog necesita las mismas. No quiero una segunda copia del bloque: sácalas a un "
               "módulo NixOS compartido en nix/modules/nixos/ e impórtalo desde los dos hosts. El sudoers que se "
               "genera para t480 no puede cambiar, y asus-rog tiene que acabar con exactamente las mismas reglas "
               "de rebuild que t480 (fuera la regla suelta que tenga ahora para nixos-rebuild si queda duplicada). "
               "Las work-vm no se tocan. Compruébalo con nix eval desde nix/."),
    "d-gke-context-why": dict(
        repo="dotfiles", commit="706cf86", cat="investigate", diff="medium",
        prompt="Añadí un contexto de kubectl gke-agent que no caduca, para no tener que hacer login en GKE cada "
               "dos por tres, pero work-vm-00 lo sigue pidiendo y `kubectl config get-contexts` no muestra "
               "gke-agent ahí. Investiga en el repo por qué work-vm-00 no lo tiene. No cambies nada: escribe en "
               "ANSWER.md qué fichero define el contexto, qué máquinas lo reciben y por qué work-vm-00 no, y qué "
               "habría que mover a dónde para que lo tenga (ojo con todo lo que hace falta para que kubectl lo vea)."),
    # ---------------------------------------------------------------- benilla (Rust, cliente 1.12)
    "b-tradeskill-nummade": dict(
        repo="benilla", commit="129b3bb8", cat="bugfix", diff="easy",
        prompt="GetTradeSkillNumMade responde 0, 0 para una fila de cabecera o un índice más allá de las filas. "
               "El cliente 1.12.1 (0x4fdc50) manda el índice malo, la cabecera y una receta sin registro de "
               "hechizo al mismo fallback (0x4fdd13), que devuelve 1 dos veces. Hazlo igual en benilla-ui y "
               "deja tests que fijen 1, 1 en esos casos. `cargo test -p benilla-ui` tiene que pasar."),
    "b-dial-ipv4": dict(
        repo="benilla", commit="8c358747", cat="feature", diff="medium",
        prompt="`localhost` resuelve primero a `::1`. benilla-protocol marca todas las direcciones resueltas en "
               "orden, así que con un realmd escuchando solo en IPv4 un login en Windows se queda unos 2 s "
               "esperando en `::1`. El cliente 1.12.1 (WowConnection::ConnectToHost, 0x5b6e40) resuelve el host "
               "a una única dirección IPv4 (inet_addr, o la primera entrada de gethostbyname) y solo marca esa; "
               "nunca IPv6. Haz que benilla haga lo mismo: un host sin dirección IPv4 falla como no resuelto. "
               "Con tests. `cargo test -p benilla-protocol` tiene que pasar."),
    "b-light-band-cycle": dict(
        repo="benilla", commit="6c25640a", cat="bugfix", diff="hard",
        prompt="Algunas filas de LightParams (por ejemplo la 8) guardan las claves de tiempo de sus bandas fuera de "
               "orden, como [720, 1440, 0], y benilla-formats (src/light/tables.rs) interpola mal esas bandas "
               "porque supone claves ascendentes. El cliente 1.12.1 (0x6d63e0) recorre las claves como un ciclo en "
               "el orden del fichero (incluido el par última -> primera): gana el primer par consecutivo que "
               "encierra el instante t, y un par que no asciende se trata como si cruzara la medianoche. Corrígelo "
               "para que coincida con el cliente, con tests. `cargo test -p benilla-formats` tiene que pasar."),
}

for _t in TASKS.values():
    _t["prompt"] += TAIL
