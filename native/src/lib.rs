//! Native SimHash signature accelerator for twinflame.
//!
//! Ports the hot path of `twinflame.signature` (the per-token hash + 32-bit
//! weighted accumulation, ~91% of fingerprinting) to native code. The Python
//! side (`signature.py`) keeps the cheap feature extraction and calls
//! `class_signature` with the four already-built `[(token, weight)]` lists.
//!
//! The mixer is MurmurHash3 x86_32 (seed 0): a fast non-cryptographic hash with
//! a strong finalizer (good avalanche — measured to keep SimHash drift on par
//! with blake2b, unlike FNV-1a). `signature.py`'s pure-Python fallback uses the
//! *same* murmur3 so a machine with the wheel and one without produce
//! bit-identical signatures.
//!
//! Build: `maturin develop --features python --release`. Import as
//! `twinflame_rs`.

const PARTIAL_BITS: usize = 32;
const MURMUR_C1: u32 = 0xcc9e2d51;
const MURMUR_C2: u32 = 0x1b873593;

/// MurmurHash3 x86_32 (seed 0) over the UTF-8 bytes of `tok`. Must stay
/// identical to `signature._hash_token`.
#[inline]
fn hash_token(tok: &str) -> u32 {
    let data = tok.as_bytes();
    let nblocks = data.len() / 4;
    let mut h: u32 = 0;
    for i in 0..nblocks {
        let j = i * 4;
        let mut k = u32::from_le_bytes([data[j], data[j + 1], data[j + 2], data[j + 3]]);
        k = k.wrapping_mul(MURMUR_C1);
        k = k.rotate_left(15);
        k = k.wrapping_mul(MURMUR_C2);
        h ^= k;
        h = h.rotate_left(13);
        h = h.wrapping_mul(5).wrapping_add(0xe654_6b64);
    }
    let tail = &data[nblocks * 4..];
    let mut k: u32 = 0;
    if tail.len() >= 3 {
        k ^= (tail[2] as u32) << 16;
    }
    if tail.len() >= 2 {
        k ^= (tail[1] as u32) << 8;
    }
    if !tail.is_empty() {
        k ^= tail[0] as u32;
        k = k.wrapping_mul(MURMUR_C1);
        k = k.rotate_left(15);
        k = k.wrapping_mul(MURMUR_C2);
        h ^= k;
    }
    h ^= data.len() as u32;
    h ^= h >> 16;
    h = h.wrapping_mul(0x85eb_ca6b);
    h ^= h >> 13;
    h = h.wrapping_mul(0xc2b2_ae35);
    h ^= h >> 16;
    h
}

/// One 32-bit SimHash over `(token, weight)` features. Empty features -> 0,
/// matching the Python contract.
#[inline]
fn simhash(feats: &[(String, f64)]) -> u32 {
    if feats.is_empty() {
        return 0;
    }
    let mut acc = [0.0f64; PARTIAL_BITS];
    for (tok, w) in feats {
        let h = hash_token(tok);
        for (i, a) in acc.iter_mut().enumerate() {
            if (h >> i) & 1 == 1 {
                *a += *w;
            } else {
                *a -= *w;
            }
        }
    }
    let mut result = 0u32;
    for (i, a) in acc.iter().enumerate() {
        if *a > 0.0 {
            result |= 1u32 << i;
        }
    }
    result
}

#[cfg(feature = "python")]
mod python {
    use super::{simhash, PARTIAL_BITS};
    use pyo3::prelude::*;

    /// Compute the four sub-signatures (cls/fld/mth/code) for one class from its
    /// four `[(token, weight)]` feature lists. The accumulation runs with the
    /// GIL released so callers can thread across classes.
    #[pyfunction]
    fn class_signature(
        py: Python<'_>,
        cls: Vec<(String, f64)>,
        fld: Vec<(String, f64)>,
        mth: Vec<(String, f64)>,
        code: Vec<(String, f64)>,
    ) -> (u32, u32, u32, u32) {
        py.detach(|| {
            (
                simhash(&cls),
                simhash(&fld),
                simhash(&mth),
                simhash(&code),
            )
        })
    }

    /// A single SimHash over `[(token, weight)]` — for parity tests against the
    /// Python fallback.
    #[pyfunction]
    fn simhash_one(py: Python<'_>, feats: Vec<(String, f64)>) -> u32 {
        py.detach(|| simhash(&feats))
    }

    #[pymodule]
    fn twinflame_rs(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_function(wrap_pyfunction!(class_signature, m)?)?;
        m.add_function(wrap_pyfunction!(simhash_one, m)?)?;
        m.add("PARTIAL_BITS", PARTIAL_BITS)?;
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn murmur3_known_vectors() {
        // MurmurHash3 x86_32, seed 0 — reference values.
        assert_eq!(hash_token(""), 0);
        assert_eq!(hash_token("a"), 0x3c2569b2);
        assert_eq!(hash_token("abc"), 0xb3dd93fa);
        assert_eq!(hash_token("Hello, world!"), 0xc0363e43);
    }

    #[test]
    fn empty_is_zero() {
        assert_eq!(simhash(&[]), 0);
    }

    #[test]
    fn single_token_sets_its_bits() {
        let h = hash_token("op=INVOKE");
        assert_eq!(simhash(&[("op=INVOKE".to_string(), 1.0)]), h);
    }
}
