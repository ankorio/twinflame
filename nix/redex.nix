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

stdenv.mkDerivation {
  pname = "redex";
  version = "2025.09.18";

  inherit src;

  nativeBuildInputs = [ cmake pkg-config python3 ];
  buildInputs = [ boost jsoncpp zlib ];

  cmakeFlags = [
    "-DCMAKE_BUILD_TYPE=Release"
    "-DBUILD_TYPE=Shared"
  ];

  # Redex's own test suite needs a full Android/JDK toolchain that isn't
  # part of this dev shell. We only need the optimizer binary.
  doCheck = false;

  meta = with lib; {
    description = "Android bytecode optimizer (LocalDcePass+RegAllocPass used by apkdiff --normalize)";
    homepage = "https://github.com/facebook/redex";
    license = licenses.mit;
    platforms = platforms.unix;
    mainProgram = "redex";
  };
}
