{
  description = "AI coding-agent usage limits (Claude Code, Codex, Kimi) for a status bar";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
    in
    {
      packages = forAllSystems (pkgs: rec {
        agent-usage = pkgs.callPackage ./default.nix { };
        default = agent-usage;
      });

      overlays.default = final: _prev: {
        agent-usage = final.callPackage ./default.nix { };
      };

      formatter = forAllSystems (pkgs: pkgs.nixpkgs-fmt);
    };
}
