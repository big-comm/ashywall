<p align="center">
  <img src="usr/share/icons/hicolor/scalable/apps/org.communitybig.ashywall.svg" width="128" height="128" alt="Ícone do AshyWall">
</p>

# AshyWall

Biblioteca de papéis de parede para o **BigCommunity GNOME**, construída com
Python, GTK4 e libadwaita. Reúne imagens do sistema, pastas pessoais e fontes
online para visualizar, organizar e aplicar no desktop. Uma seção opcional
de leitura RSS/Atom permite acompanhar sites por categoria.

A interface acompanha os temas claro e escuro do GNOME e se adapta a
janelas estreitas. O foco atual é GNOME.

## Interface

Prévia de um wallpaper do BigLinux, com céu rosa refletido em um lago.
A captura abaixo acompanha o tema usado para visualizar esta página.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/screenshots/biglinux-wallpaper-dark.png">
  <img src="docs/screenshots/biglinux-wallpaper-light.png" alt="Prévia do AshyWall com montanhas, um lago e céu rosa, na interface nativa do GNOME" width="1280">
</picture>

| Wallpaper do sistema | Coleções |
| --- | --- |
| ![Prévia do wallpaper da coruja do BigCommunity](docs/screenshots/system-wallpaper-dark.png) | ![Coleção de wallpapers coloridos instalados no sistema](docs/screenshots/collections-dark.png) |

As imagens são capturas reais do aplicativo em português, em uma sessão
isolada de demonstração. Veja [mais telas e os créditos dos wallpapers](docs/screenshots/README.md),
incluindo a galeria BigLinux e a seção de leitura.

## Destaques

- **Biblioteca local:** wallpapers do sistema, downloads e pastas escolhidas
  pelo usuário, com busca e filtros de orientação e resolução.
- **Fontes online:** BigLinux, Wallhaven, Bing e Wikimedia Commons, com as
  informações de origem, autoria e licença fornecidas por cada serviço.
- **Prévia e tela cheia:** dimensões reais, enquadramento do monitor,
  visualização da imagem inteira e tamanho real dos pixels.
- **Aplicação no GNOME:** escolha o tema claro, escuro ou ambos, com opção
  de desfazer e restaurar o wallpaper anterior.
- **Favoritos e coleções:** reúna imagens locais e online sem duplicar os
  arquivos. Remover pastas ou coleções da biblioteca não apaga suas imagens.
- **Troca automática:** use imagens locais de uma seção ou coleção, escolha
  intervalo e ordem aleatória. O GNOME continua a troca com o AshyWall fechado.
- **Leitura opcional:** fontes RSS/Atom por categoria, busca, marcação de
  lidos e não lidos e resumos em cache para consulta offline.

## Instalação

### BigCommunity

Com o repositório do BigCommunity que disponibiliza `ashywall` habilitado:

```bash
sudo pacman -Syu ashywall
```

A versão instalada acompanha o canal de pacotes configurado no sistema.
Para abrir, procure **AshyWall** no menu de aplicativos ou execute `ashywall`.

### Compilação e instalação local

Em sistemas com pacman, instale as ferramentas de compilação e obtenha o
código:

```bash
sudo pacman -S --needed base-devel git
git clone https://github.com/big-comm/ashywall.git
cd ashywall
./scripts/build-local-package.sh -si
```

O script instala as dependências declaradas na receita, compila o snapshot
Git atual, executa os testes e instala pelo pacman. Feche o aplicativo antes
de atualizar. Em um checkout com alterações, faça o commit antes de compilar.
Sem `-si`, o script apenas gera o pacote em `dist/packages/`.

### Requisitos de execução

- Python ≥ 3.12 e PyGObject.
- GTK ≥ 4.16 e libadwaita ≥ 1.6.
- Pillow ≥ 11.3 e defusedxml ≥ 0.7.1.
- GNOME e `gsettings-desktop-schemas` para aplicar wallpapers.

## Uso

1. Abra **Sistema** para ver os wallpapers instalados ou adicione suas
   pastas em **Preferências → Biblioteca → Minhas pastas**.
2. Escolha uma imagem para abrir a prévia, conferir os detalhes e aplicar
   ao tema claro, escuro ou aos dois.
3. Use os favoritos e as coleções para organizar o acervo. As fontes online
   ficam na seção **Explorar** da barra lateral.
4. No menu principal, abra **Troca automática** para escolher uma seção ou
   coleção e configurar o intervalo. Imagens online precisam ser baixadas
   antes de participar da troca.

### Leitura por categorias

Ative **Preferências → Leitura → Ativar Leitura**. Em **Fontes → Adicionar
fonte**, informe um site, como `phoronix.com`, ou o endereço HTTPS de um
RSS/Atom. Escolha uma categoria existente ou digite uma nova.

O botão de pasta muda a categoria; a lixeira remove a fonte e seus artigos
em cache após confirmação. Sem categoria preenchida, a fonte aparece em
**Sem categoria**. O filtro da leitura permite escolher a categoria desejada.

Os resumos ficam disponíveis offline; o artigo completo abre no navegador.
As fontes são consultadas ao adicionar ou atualizar manualmente, sem
notificações ou carregamento de imagens e scripts dos artigos.

### Atalhos

| Atalho | Ação |
| --- | --- |
| `Ctrl+F` | Buscar na seção atual |
| `F5` ou `Ctrl+R` | Atualizar a biblioteca ou as fontes da leitura |
| `Ctrl+,` | Abrir preferências |
| `Ctrl+Q` | Sair |

## Dados e limites atuais

A biblioteca e os downloads ficam em `~/.local/share/ashywall/`; miniaturas
e prévias em cache ficam em `~/.cache/ashywall/`. Os caminhos respeitam as
variáveis XDG quando configuradas. A pasta pessoal não é varrida automaticamente.

O GNOME usa um wallpaper por tema para todos os monitores. Aplicação por
monitor e pelo portal de wallpapers do Flatpak ainda não estão disponíveis.
Autoria e licença são exibidas quando fornecidas pela origem; o aplicativo
não concede direitos sobre as imagens.

Consulte o [guia de funcionalidades e limitações](docs/FEATURES.md) para
critérios de resolução, comportamento das fontes, cache e medidas de segurança.

## Desenvolvimento

No checkout, com as dependências de execução instaladas:

```bash
./scripts/run-dev.sh
```

Para a validação completa, instale também as ferramentas indicadas em
[CONTRIBUTING.md](CONTRIBUTING.md) e execute:

```bash
./scripts/validate.sh
```

Os testes usam dados temporários e configurações isoladas do GNOME.
[CONTRIBUTING.md](CONTRIBUTING.md) documenta o fluxo `dev-xathay` → `main`,
a publicação direta e a geração de pacotes pelo GitRepo.
[Traduções](locale/README.md) e [histórico de alterações](CHANGELOG.md)
têm documentação própria.

## Licença e autoria

[MIT](LICENSE). Desenvolvido por colaboradores do BigCommunity.
Os créditos e as licenças dos wallpapers pertencem às respectivas fontes.

O ícone provisório adapta a coruja do
[AshyTerm](https://github.com/big-comm/ashyterm) (MIT, © 2024 Community Big).
A licença dessa adaptação é instalada em
`/usr/share/licenses/ashywall/LICENSE.ashyterm-icon`.
