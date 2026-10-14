# Contributing

OLT Manager adalah internal tool yang di-publish — kontribusi welcome, tapi scope-nya
sengaja dijaga kecil (ini bukan proyek OSS besar dengan roadmap komunitas).

## Setup Lokal

    git clone <repo-url>
    cd olt-manager
    cp backend/.env.example backend/.env
    docker-compose up --build
    # atau
    ./run.sh

## Menjalankan Test

    cd backend
    pytest tests/

## Code Style

- Ikuti .editorconfig (indentasi, line ending)
- Jalankan `ruff check backend/` sebelum submit PR
- Commit message singkat & imperative — contoh: `Fix vlanif delete race condition`

## Pull Request

1. Fork, buat branch dari main
2. Satu PR = satu perubahan logis
3. Pastikan pytest dan ruff lolos lokal sebelum submit
4. Jangan pernah commit .env, *.key, atau *.db — cek git status sebelum push

## Yang Kemungkinan Ditolak

- Perubahan arsitektur besar tanpa diskusi dulu
- Dependency baru yang berat tanpa alasan kuat
- Perubahan yang menyentuh handling kredensial OLT tanpa penjelasan keamanan
