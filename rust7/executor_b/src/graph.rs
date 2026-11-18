//! Граф узлов и значения узлов (контракт, §10.4).

use sha2::{Digest, Sha256};

pub fn node_count(levels: i64) -> i64 {
    (1i64 << levels) - 1
}

/// Уровень k и номер i внутри уровня для узла с номером `j`.
fn level_of(j: i64) -> (i64, i64) {
    let mut k = 0;
    while (1i64 << (k + 1)) - 1 <= j {
        k += 1;
    }
    (k, j - ((1i64 << k) - 1))
}

/// Предки узла `j`: множество по возрастанию.
pub fn preds(graph: &str, j: i64) -> Vec<i64> {
    let (k, i) = level_of(j);
    let mut p = Vec::new();
    if k >= 1 {
        let up = (1i64 << (k - 1)) - 1;
        if graph == "M3ц" {
            p.push(up + i / 2);
            if j > 0 {
                p.push(j - 1);
            }
        } else {
            p.push(up + i / 2);
            p.push(up + (i / 2 + 1).min((1i64 << (k - 1)) - 1));
        }
    } else if graph == "M3ц" && j > 0 {
        p.push(j - 1);
    }
    p.sort();
    p.dedup();
    p
}

/// sha256(секрет ‖ "|{контур}|{узел}|" ‖ значения предков по возрастанию).
pub fn value(secret: &[u8; 32], contour: i64, j: i64, pred_values: &[[u8; 32]]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(secret);
    h.update(format!("|{contour}|{j}|").as_bytes());
    for v in pred_values {
        h.update(v);
    }
    h.finalize().into()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::json::hex;

    fn sec() -> [u8; 32] {
        let mut s = [0u8; 32];
        for (k, b) in s.iter_mut().enumerate() {
            *b = k as u8;
        }
        s
    }

    #[test]
    fn grafy() {
        let t: Vec<Vec<i64>> = (0..15).map(|j| preds("M3т", j)).collect();
        let want: Vec<Vec<i64>> = vec![
            vec![], vec![0], vec![0], vec![1, 2], vec![1, 2], vec![2], vec![2], vec![3, 4], vec![3, 4],
            vec![4, 5], vec![4, 5], vec![5, 6], vec![5, 6], vec![6], vec![6],
        ];
        assert_eq!(t, want);
        let c: Vec<Vec<i64>> = (0..7).map(|j| preds("M3ц", j)).collect();
        let want: Vec<Vec<i64>> =
            vec![vec![], vec![0], vec![0, 1], vec![1, 2], vec![1, 3], vec![2, 4], vec![2, 5]];
        assert_eq!(c, want);
    }

    #[test]
    fn znacheniya_uzlov() {
        let s = sec();
        let v0 = value(&s, 1, 0, &[]);
        assert_eq!(hex(&v0), "1c89d3c42159b4b3a623d521901500def1054530e06c1e96d3a5e36433535f18");
        let v2 = value(&s, 1, 2, &[v0]);
        assert_eq!(hex(&v2), "409e891b4ba745f60abc74ba2151f20ecf94dc82604b27040ec3288a20e5cd2b");
        let w0 = value(&s, 2, 0, &[]);
        assert_eq!(hex(&value(&s, 2, 2, &[w0])), "3d769255e9f6d3f53433cbf5a2eedbded79d9f065e1b4ee773e8d35639fd3510");
        // узел 14 с предком 6: цепочка 0 → 2 → 6 → 14
        let v6 = value(&s, 1, 6, &[v2]);
        assert_eq!(hex(&value(&s, 1, 14, &[v6])), "36b731fff4a8c6f876840ead0314fc5877f3e241b029913fc036fddcebe5084c");
    }
}
