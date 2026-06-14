{ pkgs }:

# Reporter of AI coding-agent usage limits (Claude Code, Codex, Kimi) for the
# ashell bar. Stdlib-only Python; libnotify is on PATH for `--notify`.
# Source of truth is developed in ~/projects/agent-usage and mirrored here so a
# fresh checkout of the flake builds it reproducibly.
pkgs.stdenvNoCC.mkDerivation {
  pname = "agent-usage";
  version = "0.1.0";

  src = ./.;

  nativeBuildInputs = [ pkgs.makeWrapper ];

  installPhase = ''
    runHook preInstall

    mkdir -p $out/bin $out/share/agent-usage/icons
    cp $src/agent_usage.py $out/share/agent-usage/agent_usage.py
    cp $src/icons/*.svg $out/share/agent-usage/icons/

    makeWrapper ${pkgs.python3}/bin/python3 $out/bin/agent-usage \
      --add-flags $out/share/agent-usage/agent_usage.py \
      --prefix PATH : ${pkgs.lib.makeBinPath [ pkgs.libnotify ]}

    runHook postInstall
  '';

  meta = with pkgs.lib; {
    description = "AI coding-agent usage limits (Claude Code, Codex, Kimi) for a status bar";
    mainProgram = "agent-usage";
    platforms = platforms.linux;
  };
}
