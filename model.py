import flax
import jax

from flax import linen as nn
from jax import numpy as np

import functools

from hyperparameters import Hyperparameters

JaxArray = jax.interpreters.xla._DeviceArray

class PositionalEmbedding(nn.Module):
    """Create sinusoidal positional encodings for each position in the input

    Attributes:
      hypers : Hyperparameters for the model
    """
    hypers : Hyperparameters

    def __call__(self, x):
        def sines(pos : int, i : int) -> float:
            scaled_pos = pos / (10000**(i / self.hypers.d_model))
            return np.sin(scaled_pos)
        def cosines(pos : int, i : int) -> float:
            scaled_pos = pos / (10000**(i / self.hypers.d_model))
            return np.cos(scaled_pos)

        positions = np.arange(self.hypers.seq_length)
        dimensions = np.arange(self.hypers.d_model // 2)

        pos_emb_sines   =   sines(positions[:, np.newaxis], dimensions[np.newaxis, :])
        pos_emb_cosines = cosines(positions[:, np.newaxis], dimensions[np.newaxis, :])

        pos_emb = np.concatenate([pos_emb_sines, pos_emb_cosines], axis=1)
        pos_emb = np.expand_dims(pos_emb, axis=0)

        assert(pos_emb.shape[1:] == x.shape[1:])

        return x + pos_emb


class TransformerEmbedding(nn.Module):
    """Embed previously tokenized input into vectors of size d_model
    
    Attributes:
      hypers : Hyperparameters for the model
      shared_embedding : An nn.Embed module shared between the input and output embeddings, and the decoding linear transformation
    """
    hypers : Hyperparameters
    shared_embedding : nn.Embed

    @nn.compact
    def __call__(self, x : JaxArray) -> JaxArray:
        x = self.shared_embedding(x)
        x = PositionalEmbedding(hypers=self.hypers)(x)

        return x

class FeedForward(nn.Module):
    """FeedForward block with two dense networks

    Attributes:
      hypers : Hyperparameters for the model
    """
    hypers : Hyperparameters

    @nn.compact
    def __call__(self, x : JaxArray) -> JaxArray:
        x = nn.Dense(features=self.hypers.d_model)(x)
        x = nn.Dense(features=self.hypers.d_model)(x)

        return x

class Encoder(nn.Module):
    """Encoder layer

    Attributes:
      hypers : Hyperparameters for the model
    """
    hypers : Hyperparameters

    @nn.compact
    def __call__(self, x : JaxArray) -> JaxArray:
        multihead_residual = x
        x = nn.attention.MultiHeadDotProductAttention(
                num_heads=self.hypers.num_heads,
                dropout_rate=self.hypers.training_attn_dropout,
                deterministic=self.hypers.deterministic)(x, x)
        x = x + multihead_residual
        x = nn.LayerNorm()(x)

        feed_forward_residual = x
        x = FeedForward(self.hypers)(x)
        x = x + feed_forward_residual
        x = nn.LayerNorm()(x)

        return x

class Decoder(nn.Module):
    """Decoder layer

    Attributes:
      hypers : Hyperparameters for the model
    """
    hypers : Hyperparameters

    @nn.compact
    def __call__(self,
            x : JaxArray,
            decoder_mask : JaxArray,
            encoder_output : JaxArray) -> JaxArray:

        # Compute the decoder multi-headed self-attention
        multihead_residual = x
        x = nn.attention.MultiHeadDotProductAttention(
                num_heads=self.hypers.num_heads,
                dropout_rate=self.hypers.training_attn_dropout,
                deterministic=self.hypers.deterministic)(x, x, decoder_mask)
        x = x + multihead_residual
        x = nn.LayerNorm()(x)

        # Compute the encoder-decoder attention
        # Encoder provides the values and the keys
        # Decoder provides the queries
        encoder_decoder_residual = x
        x = nn.attention.MultiHeadDotProductAttention(
                num_heads=self.hypers.num_heads,
                dropout_rate=self.hypers.training_attn_dropout,
                deterministic=self.hypers.deterministic)(x, encoder_output)
        x = x + encoder_decoder_residual
        x = nn.LayerNorm()(x)

        # Pass everything through a feed-forward layer, with
        # residuals and layer normalisation
        feed_forward_residual = x
        x = FeedForward(self.hypers)(x)
        x = x + feed_forward_residual
        x = nn.LayerNorm()(x)

        return x

class Transformer(nn.Module):
    """Full transformer model à la Vaswani et al. 

    Attributes:
      hypers : Hyperparameters for the model
    """
    hypers : Hyperparameters

    @nn.compact
    def __call__(self,
            x : JaxArray,
            decoded_seq : JaxArray) -> JaxArray:
        # Create an embedding to be shared between the input/output encoding,
        # and the decoding blocks
        shared_embedding = nn.Embed(num_embeddings=self.hypers.vocabulary_size,
                                    features=self.hypers.d_model)

        # Embed the input (includes positional embedding)
        x = TransformerEmbedding(self.hypers, shared_embedding)(x)

        # Pass the embedded input through the stack of encoders
        for _ in range(self.hypers.num_encoders):
            x = Encoder(self.hypers)(x)


        # Save the output of the encoder
        encoder_output = x


        # Shift the previous transformer output right
        x = decoded_seq
        x = np.pad(x, [(0,0), (1,0)], mode='constant', constant_values=0)

        # Trim the furthest right value off
        x = x[:, :-1]

        # Embed the past output (including positional embedding)
        # Use the shared embedding
        x = TransformerEmbedding(self.hypers, shared_embedding)(x)

        # Make a standard decoder mask
        decoder_mask = nn.attention.make_causal_mask(decoded_seq)

        # Pass the output of the encoder and the previous decoder
        # output through the stack of decoders
        for _ in range(self.hypers.num_decoders):
            x = Decoder(self.hypers)(x, decoder_mask, encoder_output)

        # Use the transform of the shared embedding to decode the output
        x = shared_embedding.attend(x)

        # Scale the output according to Vaswani et al.
        x = x / np.sqrt(self.hypers.d_model)

        # Apply a softmax to obtain relative probabilities
        x = nn.softmax(x)

        return x


