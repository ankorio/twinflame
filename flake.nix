{
  description = "apkdiff — DEX-level Android APK class-diffing engine";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
    # Redex isn't in nixpkgs; vendor it as a flake input + custom derivation
    # at ./nix/redex.nix. The v2025.09.18 release tag is unusable as-shipped:
    # redex.py references an undeclared `stub_resource_optimizations` arg
    # (since fixed in commit f2cc844), uses the Python-3.13-removed `pipes`
    # module (b9c7d5a), and has the `--redex-binary` autodetect bug (6b6d942).
    # We pin to a `main` commit that includes all three fixes; flake.lock
    # locks the exact SHA for reproducibility.
    redex-src = {
      url = "github:facebook/redex/main";
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
          rapidfuzz
        ];

        pyTestDeps = ps: with ps; [
          pytest
          pytest-cov
          hypothesis
        ];

        # Redex is vendored from ./nix/redex.nix using the pinned
        # `redex-src` flake input — nixpkgs does not package it. Defined
        # before `apkdiff` because `apkdiff`'s wrapper bakes it into PATH.
        # Pin Python explicitly: redex.py uses the `pipes` stdlib module
        # which was removed in Python 3.13, so we must stay on 3.12.
        redex = pkgs.callPackage ./nix/redex.nix {
          src = redex-src;
          python3 = python;
        };

        apkdiff = python.pkgs.buildPythonApplication {
          pname = "apkdiff";
          version = "0.1.0";
          src = ./.;
          format = "pyproject";
          nativeBuildInputs = with python.pkgs; [ setuptools wheel ];
          propagatedBuildInputs = pyDeps python.pkgs;
          nativeCheckInputs = pyTestDeps python.pkgs;
          pythonImportsCheck = [ "apkdiff" ];

          # `--normalize` shells out to `redex`. The dev shell has it on PATH
          # already; for `nix run` / `nix build` the resulting binary needs
          # Redex baked into its wrapper PATH too.
          makeWrapperArgs = [ "--prefix" "PATH" ":" "${redex}/bin" ];
        };

        # Tools the analyst uses interactively (jadx for decompilation) but
        # which the library itself doesn't shell out to.
        analystTools = [
          pkgs.jadx
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
            redex
          ] ++ analystTools;

          shellHook = ''
            echo "apkdiff dev shell — python $(python --version | cut -d' ' -f2), redex on PATH"
          '';
        };
      });
}
