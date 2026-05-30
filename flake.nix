{
  description = "apkdiff — DEX-level Android APK class-diffing engine";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    # Redex isn't in nixpkgs; vendor it as a flake input + custom derivation
    # at ./nix/redex.nix. Pinned to a tagged release for reproducibility.
    redex-src = {
      url = "github:facebook/redex/v2025.09.18";
      flake = false;
    };
  };

  outputs = { nixpkgs, flake-utils, redex-src, ... }:
    flake-utils.lib.eachSystem [ "x86_64-linux" "aarch64-darwin" ] (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          config = {
            # androguard has a transitive dep on `dataset`, which is currently
            # marked broken in nixpkgs-unstable because its pinned sqlalchemy
            # version is older than what nixpkgs ships. We override below to
            # skip the runtime deps check (androguard itself works fine).
            allowBroken = true;
          };
          overlays = [
            (final: prev: {
              python312 = prev.python312.override {
                packageOverrides = pyFinal: pyPrev: {
                  dataset = pyPrev.dataset.overridePythonAttrs (old: {
                    meta = (old.meta or { }) // { broken = false; };
                    dontCheckRuntimeDeps = true;
                    doCheck = false;
                  });
                  androguard = pyPrev.androguard.overridePythonAttrs (old: {
                    # Upstream androguard's test suite is wired to sqlalchemy<2,
                    # but nixpkgs ships sqlalchemy 2.x. The library itself works
                    # for our use case (DEX/APK parsing); only its own session
                    # API hits the SA 2.x incompatibility. Skip the check phase.
                    doCheck = false;
                    dontCheckRuntimeDeps = true;
                  });
                };
              };
            })
          ];
        };
        python = pkgs.python312;

        # Runtime Python deps (mirrored in pyproject.toml — see plan; acceptable
        # duplication for buildPythonApplication simplicity).
        pyDeps = ps: with ps; [
          androguard
          numpy
        ];

        pyTestDeps = ps: with ps; [
          pytest
          pytest-cov
          hypothesis
        ];

        apkdiff = python.pkgs.buildPythonApplication {
          pname = "apkdiff";
          version = "0.1.0";
          src = ./.;
          format = "pyproject";
          nativeBuildInputs = with python.pkgs; [ setuptools wheel ];
          propagatedBuildInputs = pyDeps python.pkgs;
          nativeCheckInputs = pyTestDeps python.pkgs;
          # Tests require synthetic-DEX fixture generation; safe inside the
          # sandbox once they don't shell out.
          pythonImportsCheck = [ "apkdiff" ];
        };

        # Redex is vendored from ./nix/redex.nix using the pinned
        # `redex-src` flake input — nixpkgs does not package it.
        redex = pkgs.callPackage ./nix/redex.nix { src = redex-src; };

        # Analyst-side tools (only needed in the dev shell, never at runtime
        # of the library).
        analystTools = [
          pkgs.jadx
          redex
        ];

      in {
        packages.default = apkdiff;
        packages.apkdiff = apkdiff;

        apps.default = {
          type = "app";
          program = "${apkdiff}/bin/apkdiff";
        };

        devShells.default = pkgs.mkShell {
          packages = [
            (python.withPackages (ps: pyDeps ps ++ pyTestDeps ps))
            pkgs.uv
          ] ++ analystTools;

          shellHook = ''
            echo "apkdiff dev shell — python $(python --version | cut -d' ' -f2), redex $(redex --version 2>/dev/null | head -1 || echo '?')"
          '';
        };
      });
}
