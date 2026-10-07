"""Moteur d'EXÉCUTION LIVE de l'arbitrage inter-DEX — Phase 2 (NON ACTIVÉE).

CE FICHIER NE CONTIENT ENCORE AUCUN CODE D'EXÉCUTION FONCTIONNEL.
C'est un squelette d'architecture : chaque fonction documente précisément ce
qu'elle fera et lève `NotImplementedError`. Objectif : réfléchir/valider le
design AVANT d'écrire le moindre code qui pourrait toucher à du capital réel
(demande explicite de l'utilisateur : "commence à réfléchir à l'architecture
de la Phase 2, sans encore l'activer").

Rien ici n'est câblé au scheduler ni à aucune commande CLI : ce module est
actuellement mort (jamais importé en dehors de lui-même), par sécurité.

─────────────────────────────────────────────────────────────────────────────
CONTEXTE ET CONTRAINTES VALIDÉES AVEC L'UTILISATEUR
─────────────────────────────────────────────────────────────────────────────
- Capital de test : 5 $ (voir config.yaml `trading.max_trade_size_usd: 3`).
- Wallet : le wallet MetaMask PRINCIPAL de l'utilisateur (décision explicite,
  risque signalé à deux reprises : la clé privée exportée sur le Pi expose
  TOUT le solde du wallet, pas seulement les 5 $ alloués à ce test — accepté
  en connaissance de cause).
- Non-négociable : tant que `trading.enabled` reste `false` dans config.yaml,
  aucune des fonctions ci-dessous ne doit jamais être appelée par le reste du
  code. Le passage à `true` doit lui-même exiger une confirmation explicite
  supplémentaire au moment de l'implémentation réelle (voir `confirm_go_live`
  plus bas) — pas un simple changement de config silencieux.

─────────────────────────────────────────────────────────────────────────────
PIÈCES MANQUANTES AVANT TOUTE IMPLÉMENTATION RÉELLE (à construire une par une,
avec validation explicite de l'utilisateur à chaque étape sensible)
─────────────────────────────────────────────────────────────────────────────
1. IMPORT DE LA CLÉ PRIVÉE DU WALLET PRINCIPAL
   `app/wallet/signer.py` sait aujourd'hui uniquement GÉNÉRER un nouveau
   wallet dédié chiffré (`create_autosigner_wallet`). Il faudra une nouvelle
   fonction `import_wallet_from_private_key(private_key, passphrase)` qui :
     - ne doit JAMAIS afficher/logger la clé privée en clair, même une fois,
     - chiffre immédiatement avec le même format keystore (eth-account /
       `Account.encrypt`) que l'autosigner existant,
     - doit être appelée UNE SEULE FOIS, manuellement, directement sur le Pi
       (jamais via une commande distante/scriptée automatiquement), avec
       suppression immédiate de la clé en clair de l'historique shell/.env
       une fois le keystore créé.
   Tant que cette fonction n'existe pas et n'a pas été exécutée manuellement
   par l'utilisateur, aucune transaction ne peut techniquement être signée.

2. ADRESSES DES ROUTERS DE SWAP (À VÉRIFIER, jamais à deviner)
   Chaque DEX whitelisté a un contrat "router" dont la fonction de swap doit
   être appelée avec les bons paramètres (minAmountOut notamment, pour une
   protection anti-slippage au niveau du contrat lui-même, pas seulement
   dans nos calculs). Adresses à vérifier individuellement (doc officielle +
   BaseScan vérifié + recoupement avec une 2e source) avant tout usage réel,
   suivant la même rigueur que pour les seed_tokens de arbitrage.py :
     - Uniswap V3 SwapRouter02 (Base)
     - Aerodrome Router (Base)
     - SushiSwap Router (Base)
     - PancakeSwap SmartRouter (Base)

3. GARDE-FOUS SPÉCIFIQUES AU TRADING (extension de app/wallet/autosign.py)
   `send_guarded_transaction()` existant est conçu pour UN appel de contrat
   isolé (claim). L'arbitrage nécessite DEUX swaps séquentiels (non-atomiques
   sans contrat dédié — voir point 5) avec des garde-fous supplémentaires :
     - plafond strict par trade = `trading.max_trade_size_usd` (3 $ pour ce
       test), jamais dépassable même si le signal calculé suggère plus,
     - plafond de solde réel : ne jamais trader plus que le solde ACTUELLEMENT
       disponible dans le wallet (lecture on-chain juste avant d'agir, pas de
       confiance dans une valeur en cache),
     - whitelist stricte des adresses de router (point 2) — jamais d'adresse
       de contrat arbitraire,
     - simulation obligatoire (`eth_call`) de chaque swap AVANT envoi réel,
     - vérification du prix juste avant la 2e transaction (le marché a pu
       bouger depuis la détection) : annuler le round-trip si le profit net
       recalculé est tombé sous le seuil,
     - limite de transactions/jour réutilisée telle quelle (AUTOSIGN_MAX_TX_PER_DAY),
     - le killswitch existant (`app.killswitch.is_stopped`) coupe tout, comme
       pour l'auto-signature actuelle.

4. RISQUE NON-ATOMIQUE (2 transactions séparées, pas une seule)
   Sans contrat dédié (point 5), le round-trip est : swap sur `buy_dex` PUIS
   swap sur `sell_dex`, dans deux transactions distinctes. Entre les deux, le
   prix peut bouger et la 2e jambe devenir perdante. Mitigation prévue :
     - ne tenter le round-trip QUE si le profit net estimé dépasse largement
       le seuil minimal (marge de sécurité supplémentaire, à calibrer avec
       les données de simulation accumulées),
     - revérifier le prix juste avant la 2e transaction (point 3),
     - accepter le risque résiduel qu'une seule jambe soit exécutée en cas de
       mouvement brutal — sur un capital de 5 $, la perte maximale possible
       dans ce scénario reste bornée et faible, mais doit être explicitement
       acceptée avant activation.

5. (Hors scope immédiat) CONTRAT D'ARBITRAGE ATOMIQUE — Phase 3 distincte
   Un contrat Solidity qui enchaîne les deux swaps dans UNE seule transaction
   (annulée entièrement si non-rentable) éliminerait le risque du point 4.
   Nécessite déploiement et financement d'un contrat : phase à part entière,
   non commencée, nécessitant un accord explicite séparé.

6. CONFIRMATION DE PASSAGE EN LIVE
   `confirm_go_live()` ci-dessous doit exiger une confirmation explicite et
   non-ambiguë (ex: variable d'environnement dédiée + phrase de confirmation
   tapée manuellement), pas uniquement `trading.enabled: true` dans un fichier
   de config qui pourrait être modifié par erreur.

─────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

from app.database import Database


class LiveExecutionNotImplemented(Exception):
    """Levée par toute fonction de ce module : aucune exécution live n'existe encore."""


def confirm_go_live(db: Database, config: dict) -> bool:
    """[SQUELETTE] Vérifiera, en plus de `trading.enabled: true`, une
    confirmation explicite distincte (ex: variable d'environnement
    `TRADING_LIVE_CONFIRMED=<phrase exacte choisie avec l'utilisateur>`)
    avant d'autoriser quoi que ce soit dans ce module. Retourne toujours
    False tant que ce n'est pas implémenté."""
    raise LiveExecutionNotImplemented(
        "Phase 2 non implémentée : aucune confirmation de passage en live n'existe encore."
    )


def execute_signal(db: Database, config: dict, signal: dict) -> dict:
    """[SQUELETTE] Exécuterait le round-trip (swap buy_dex -> swap sell_dex)
    pour un signal d'arbitrage marqué `would_execute=True`, avec tous les
    garde-fous listés en tête de fichier. Ne fait rien d'autre qu'expliquer
    pourquoi ce n'est pas encore possible."""
    raise LiveExecutionNotImplemented(
        "Phase 2 non implémentée : aucune transaction d'arbitrage n'est envoyée. "
        "Voir app/trading/executor.py pour la liste des prérequis avant activation."
    )
