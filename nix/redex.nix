{ lib
, stdenv
, src
, cmake
, pkg-config
, boost
, jsoncpp
, zlib
, python3
}:

let
  # Redex's Python wrapper (redex.py) imports `packaging.version`. Bundle a
  # Python interpreter with that dep so the shebang resolves cleanly when
  # invoked from outside a venv (e.g. via `nix run apkdiff -- --normalize`).
  pythonWithDeps = python3.withPackages (ps: with ps; [ packaging ]);
in
stdenv.mkDerivation {
  pname = "redex";
  version = "2025.09.18";

  inherit src;

  nativeBuildInputs = [ cmake pkg-config pythonWithDeps ];
  buildInputs = [ boost jsoncpp zlib ];

  cmakeFlags = [
    "-DCMAKE_BUILD_TYPE=Release"
    "-DBUILD_TYPE=Shared"
  ];

  # After all passes complete, Redex runs a final IRTypeChecker that has no
  # JSON/CLI knob to disable (m_checker_disabled is "FOR TESTING ONLY", not
  # bound to the config system — see libredex/PassManager.h). Proguard-output
  # bytecode commonly violates this checker because Dalvik allows generic-erased
  # Object↔CharSequence flow that Java semantics don't. We don't care about
  # type soundness when we're only stripping junk instructions, so make the
  # final check non-fatal: pass exit_on_fail=false so it reports rather than
  # asserts. The optimizer then proceeds to emit the normalized APK.
  postPatch = ''
    substituteInPlace libredex/PassManager.cpp \
      --replace-fail '.run_verifier(scope);' '.run_verifier(scope, false);'
  '';

  # Redex's own test suite needs a full Android/JDK toolchain that isn't
  # part of this dev shell. We only need the optimizer binary.
  doCheck = false;

  # `redex.py` is the user-facing Python wrapper around `redex-all` (the C++
  # binary). Three fixups are needed for it to work in a sealed Nix store:
  #   1. Install drops it without an executable bit.
  #   2. Its shebang is `#!/usr/bin/env python3`; we pin it to a Python env
  #      that bundles `packaging` (Facebook's setup_oss_toolchain.sh installs
  #      it system-wide via apt — we have no shared site-packages).
  #   3. `redex.py`'s argparse layer raises if `--redex-binary` isn't
  #      passed, even though `redex-all` sits right next to it. We expose
  #      the user-facing `redex` as a shim that injects that argument so
  #      callers like `subprocess.run(["redex", ...])` (see
  #      src/apkdiff/normalize.py) can stay oblivious.
  postInstall = ''
    substituteInPlace $out/bin/redex.py \
      --replace-fail '#!/usr/bin/env python3' '#!${pythonWithDeps}/bin/python3'
    chmod +x $out/bin/redex.py
    cat > $out/bin/redex <<EOF
    #!/bin/sh
    # --ignore-zipalign / --ignore-apksigner: apkdiff consumes the optimized
    # DEX bytes for structural comparison; we don't need a runtime-loadable
    # APK, so missing Android build-tools (zipalign, apksigner) shouldn't be
    # fatal. With these flags the wrapper falls back to a plain copy of the
    # rezipped APK rather than refusing to emit output.
    exec $out/bin/redex.py \\
      --redex-binary $out/bin/redex-all \\
      --ignore-zipalign \\
      --ignore-apksigner \\
      "\$@"
    EOF
    chmod +x $out/bin/redex
  '';

  meta = with lib; {
    description = "Android bytecode optimizer (LocalDcePass+RegAllocPass used by apkdiff --normalize)";
    homepage = "https://github.com/facebook/redex";
    license = licenses.mit;
    platforms = platforms.unix;
    mainProgram = "redex";
  };
}
