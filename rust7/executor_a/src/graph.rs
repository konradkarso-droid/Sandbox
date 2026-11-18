//! Граф узлов и значение узла.

use sha2::{Digest, Sha256};

/// Число узлов N = 2^K − 1.
pub fn size(levels: u32) -> usize {
    (1usize << levels) - 1
}

/// Предки узла j — множество по возрастанию.
pub fn preds(graph: &str, j: usize) -> Vec<usize> {
    let k = (usize::BITS - 1 - (j + 1).leading_zeros()) as usize; // уровень: 2^k − 1 ≤ j
    let i = j - ((1usize << k) - 1);
    let mut p = Vec::new();
    if k >= 1 {
        let up = (1usize << (k - 1)) - 1;
        if graph == "M3ц" {
            p.push(up + i / 2);
            if j > 0 {
                p.push(j - 1);
            }
        } else {
            p.push(up + i / 2);
            p.push(up + std::cmp::min(i / 2 + 1, (1usize << (k - 1)) - 1));
        }
    } else if graph == "M3ц" && j > 0 {
        p.push(j - 1);
    }
    p.sort();
    p.dedup();
    p
}

/// sha256(секрет ‖ "|{контур}|{j}|" ‖ значения предков по возрастанию).
pub fn node_value(secret: &[u8; 32], contour: usize, j: usize, pred_values: &[[u8; 32]]) -> [u8; 32] {
    let mut h = Sha256::new();
    h.update(secret);
    h.update(format!("|{}|{}|", contour, j).as_bytes());
    for v in pred_values {
        h.update(v);
    }
    h.finalize().into()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::canon::hex;

    #[test]
    fn graphs() {
        let t: Vec<Vec<usize>> = (0..15).map(|j| preds("M3т", j)).collect();
        let want: Vec<Vec<usize>> = vec![
            vec![], vec![0], vec![0], vec![1, 2], vec![1, 2], vec![2], vec![2], vec![3, 4], vec![3, 4],
            vec![4, 5], vec![4, 5], vec![5, 6], vec![5, 6], vec![6], vec![6],
        ];
        assert_eq!(t, want);
        let c: Vec<Vec<usize>> = (0..7).map(|j| preds("M3ц", j)).collect();
        let want: Vec<Vec<usize>> = vec![vec![], vec![0], vec![0, 1], vec![1, 2], vec![1, 3], vec![2, 4], vec![2, 5]];
        assert_eq!(c, want);
    }

    #[test]
    fn values() {
        let mut s = [0u8; 32];
        for (k, b) in s.iter_mut().enumerate() {
            *b = k as u8;
        }
        let v0 = node_value(&s, 1, 0, &[]);
        assert_eq!(hex(&v0), "1c89d3c42159b4b3a623d521901500def1054530e06c1e96d3a5e36433535f18");
        let v2 = node_value(&s, 1, 2, &[v0]);
        assert_eq!(hex(&v2), "409e891b4ba745f60abc74ba2151f20ecf94dc82604b27040ec3288a20e5cd2b");
        let w0 = node_value(&s, 2, 0, &[]);
        let w2 = node_value(&s, 2, 2, &[w0]);
        assert_eq!(hex(&w2), "3d769255e9f6d3f53433cbf5a2eedbded79d9f065e1b4ee773e8d35639fd3510");
    }
}
